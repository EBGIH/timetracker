/* Kiosk client (Module D).

   Single purpose, no navigation chrome, no route to any other part of the
   system. Events are queued locally when offline and replayed with their
   original timestamps on reconnection, each carrying an idempotency key so a
   replay cannot double-book (FR-D-09, US-01 AC-5). */

const token = new URLSearchParams(location.search).get('token') || '';
const root = document.getElementById('kiosk');
const QUEUE_KEY = `tk_kiosk_queue_${token.slice(0, 8)}`;

let config = null;
let roster = [];
let breakTypes = [];
let selected = null;
let pin = '';
let online = navigator.onLine;

/* ---------- Tiny DOM helper (kept local so the kiosk ships alone) ---------- */

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(), value);
    else node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === 'object' ? child : document.createTextNode(String(child)));
  }
  return node;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }

function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

/* ---------- Offline queue ---------- */

function readQueue() {
  try { return JSON.parse(localStorage.getItem(QUEUE_KEY) || '[]'); } catch { return []; }
}

function writeQueue(items) {
  localStorage.setItem(QUEUE_KEY, JSON.stringify(items));
}

function enqueue(event) {
  const queue = readQueue();
  queue.push(event);
  writeQueue(queue);
  renderHeaderStatus();
}

async function flushQueue() {
  const queue = readQueue();
  if (!queue.length) return;
  try {
    const response = await fetch(`/api/kiosk/sync?token=${encodeURIComponent(token)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ events: queue }),
    });
    if (!response.ok) return;
    writeQueue([]);
    renderHeaderStatus();
    await loadConfig();
  } catch { /* still offline */ }
}

/* ---------- Data ---------- */

async function loadConfig() {
  const response = await fetch(`/api/kiosk/session?token=${encodeURIComponent(token)}`);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.message || 'This kiosk link is not valid.');
  }
  const data = await response.json();
  config = data.kiosk;
  roster = data.roster;
  breakTypes = data.break_types;
  return data;
}

async function send(action, extra = {}) {
  const event = {
    user_id: selected.id,
    pin: pin || null,
    action,
    idempotency_key: uuid(),
    occurred_at: new Date().toISOString(),
    device_id: deviceId(),
    ...extra,
  };
  if (!navigator.onLine) {
    enqueue(event);
    return { status: `${action}_queued`, user_name: selected.name, at: event.occurred_at };
  }
  const response = await fetch(`/api/kiosk/event?token=${encodeURIComponent(token)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(event),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.message || 'Not recognised — please try again');
    error.payload = payload;
    throw error;
  }
  return payload;
}

function deviceId() {
  let id = localStorage.getItem('tk_device_id');
  if (!id) { id = uuid(); localStorage.setItem('tk_device_id', id); }
  return id;
}

/* ---------- Screens ---------- */

function header() {
  const clock = el('div', { class: 'k-clock', id: 'k-clock' }, '--:--');
  return el('div', { class: 'k-head' },
    el('h1', {}, config?.name || 'Clock in'),
    el('div', { class: 'spacer' }),
    el('div', { class: 'k-status', id: 'k-status' }, ''),
    clock);
}

function renderHeaderStatus() {
  const node = document.getElementById('k-status');
  if (!node) return;
  const queued = readQueue().length;
  if (!online) {
    node.className = 'k-status offline';
    node.textContent = queued
      ? `Offline — ${queued} entr${queued === 1 ? 'y' : 'ies'} saved on this device`
      : 'Offline — entries will be saved and sent later';
  } else if (queued) {
    node.className = 'k-status offline';
    node.textContent = `Sending ${queued} saved entr${queued === 1 ? 'y' : 'ies'}…`;
  } else {
    node.className = 'k-status';
    node.textContent = '';
  }
}

function tickClock() {
  const node = document.getElementById('k-clock');
  if (node) {
    node.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
}

function renderRoster() {
  selected = null;
  pin = '';
  const tiles = el('div', { class: 'roster' });
  for (const person of roster) {
    tiles.appendChild(el('button', {
      class: `tile ${person.status}`,
      onclick: () => renderPad(person),
    },
      el('div', { class: 'tname' }, person.name),
      el('div', { class: 'tstate' },
        person.status === 'in' ? 'Clocked in'
          : person.status === 'on_break' ? 'On break' : 'Not clocked in')));
  }
  clear(root).append(header(), el('div', { class: 'k-body' },
    roster.length ? tiles : el('div', { class: 'k-error' },
      'No employees are assigned to this kiosk yet.')));
  renderHeaderStatus();
  tickClock();
}

function renderPad(person) {
  selected = person;
  pin = '';
  const digits = config.auth_method === 'pin6' ? 6 : 4;
  const dots = el('div', { class: 'pin-dots' },
    Array.from({ length: digits }, () => el('span', {})));
  const errorBox = el('div', {});

  function refresh() {
    [...dots.children].forEach((dot, index) => {
      dot.className = index < pin.length ? 'filled' : '';
    });
    if (pin.length === digits) submit();
  }

  async function submit() {
    const action = person.status === 'out' ? 'clock_in' : 'clock_out';
    clear(errorBox);
    try {
      const result = await send(action);
      renderConfirmation(action, result);
    } catch (error) {
      pin = '';
      refresh();
      errorBox.appendChild(el('div', { class: 'k-error' }, error.message));
      if (error.payload?.error === 'already_clocked_in') person.status = 'in';
      if (error.payload?.error === 'not_clocked_in') person.status = 'out';
    }
  }

  const keys = el('div', { class: 'keys' });
  for (const key of ['1', '2', '3', '4', '5', '6', '7', '8', '9']) {
    keys.appendChild(el('button', {
      onclick: () => { if (pin.length < digits) { pin += key; refresh(); } },
    }, key));
  }
  keys.append(
    el('button', { class: 'wide', onclick: () => { pin = ''; refresh(); } }, 'Clear'),
    el('button', {
      onclick: () => { if (pin.length < digits) { pin += '0'; refresh(); } },
    }, '0'),
    el('button', {
      class: 'wide', onclick: () => { pin = pin.slice(0, -1); refresh(); },
    }, 'Delete'),
  );

  /* FR-D-05 / US-01 AC-4: the primary action reflects the current state. */
  const primaryLabel = person.status === 'out' ? 'Clock in' : 'Clock out';
  const breakButtons = config.breaks_enabled && person.status !== 'out'
    ? el('div', { class: 'breaks' },
      person.status === 'on_break'
        ? el('button', {
          class: 'neutral', style: 'min-height:68px;font-size:1.2rem;border-radius:14px;cursor:pointer',
          onclick: async () => {
            if (pin.length < digits) {
              errorBox.appendChild(el('div', { class: 'k-error' }, 'Enter your PIN first.'));
              return;
            }
            try { renderConfirmation('break_end', await send('break_end')); }
            catch (error) { errorBox.appendChild(el('div', { class: 'k-error' }, error.message)); }
          },
        }, 'End break')
        : breakTypes.map((type) => el('button', {
          class: 'neutral', style: 'min-height:68px;font-size:1.2rem;border-radius:14px;cursor:pointer;width:100%',
          onclick: async () => {
            if (pin.length < digits) {
              errorBox.appendChild(el('div', { class: 'k-error' }, 'Enter your PIN first.'));
              return;
            }
            try {
              renderConfirmation('break_start', await send('break_start', { break_type_id: type.id }));
            } catch (error) { errorBox.appendChild(el('div', { class: 'k-error' }, error.message)); }
          },
        }, `Start ${type.name}`)))
    : null;

  clear(root).append(header(), el('div', { class: 'k-body' },
    el('div', { class: 'pad' },
      el('h2', {}, person.name),
      el('p', { style: 'font-size:1.15rem;color:var(--k-muted);margin-top:0' },
        `Enter your ${digits}-digit PIN to ${primaryLabel.toLowerCase()}`),
      dots, keys, errorBox, breakButtons,
      el('button', { class: 'k-back', onclick: renderRoster }, '← Someone else'))));
  renderHeaderStatus();
  tickClock();
}

function renderConfirmation(action, result) {
  const kind = action === 'clock_in' || action === 'break_end' ? 'in' : 'out';
  const titles = {
    clock_in: 'Clocked in', clock_out: 'Clocked out',
    break_start: 'Break started', break_end: 'Break ended',
  };
  const queued = String(result.status || '').endsWith('_queued');
  const time = result.at
    ? new Date(result.at.endsWith('Z') ? result.at : result.at + 'Z')
      .toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

  clear(root).append(header(), el('div', { class: 'k-body', style: 'display:flex' },
    el('div', { class: `confirm ${kind}` },
      el('div', { class: 'mark' }, '✓'),
      el('h2', {}, titles[action] || 'Recorded'),
      el('p', {}, result.user_name || ''),
      el('div', { class: 'big-time' }, time),
      result.worked_minutes
        ? el('p', {}, `${Math.floor(result.worked_minutes / 60)} h `
          + `${String(result.worked_minutes % 60).padStart(2, '0')} min recorded today`)
        : null,
      queued ? el('p', {}, 'Saved on this device — it will be sent when the network returns.') : null)));
  renderHeaderStatus();
  tickClock();
  /* US-01 AC-2: the confirmation stays up for at least three seconds. */
  setTimeout(async () => {
    try { await loadConfig(); } catch { /* keep the last roster */ }
    renderRoster();
  }, 3500);
}

/* ---------- Bootstrap ---------- */

async function start() {
  if (!token) {
    clear(root).append(el('div', { class: 'k-error' },
      'This page needs a kiosk link. Ask an administrator to launch the kiosk.'));
    return;
  }
  try {
    await loadConfig();
  } catch (error) {
    clear(root).append(el('div', { class: 'k-error' }, error.message));
    return;
  }
  renderRoster();
  setInterval(tickClock, 1000);
  setInterval(async () => {
    if (!selected && navigator.onLine) {
      try { await loadConfig(); renderRoster(); } catch { /* ignore */ }
    }
  }, 45000);
  setInterval(() => { if (navigator.onLine) flushQueue(); }, 20000);
  window.addEventListener('online', () => { online = true; renderHeaderStatus(); flushQueue(); });
  window.addEventListener('offline', () => { online = false; renderHeaderStatus(); });
  flushQueue();
}

start();
