/* API client, session handling and shared formatting helpers. */

export const state = {
  token: localStorage.getItem('tk_token') || null,
  user: null,
};

class ApiError extends Error {
  constructor(status, payload) {
    super(payload?.message || payload?.detail || `Request failed (${status})`);
    this.status = status;
    this.payload = payload || {};
  }
}

export { ApiError };

async function request(method, path, body, options = {}) {
  const headers = { Accept: 'application/json' };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';

  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (response.status === 401 && !options.allowAnonymous) {
    signOut(true);
    throw new ApiError(401, { message: 'Your session has expired. Please sign in again.' });
  }
  if (options.raw) {
    if (!response.ok) throw new ApiError(response.status, await safeJson(response));
    return response;
  }
  const payload = await safeJson(response);
  if (!response.ok) throw new ApiError(response.status, payload);
  return payload;
}

async function safeJson(response) {
  const text = await response.text();
  if (!text) return null;
  try { return JSON.parse(text); } catch { return { message: text }; }
}

export const api = {
  get: (path, options) => request('GET', path, undefined, options),
  post: (path, body, options) => request('POST', path, body ?? {}, options),
  put: (path, body) => request('PUT', path, body ?? {}),
  del: (path) => request('DELETE', path),
  async download(path, body, filename) {
    const response = await request('POST', path, body ?? {}, { raw: true });
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  },
  async downloadGet(path, filename) {
    const response = await request('GET', path, undefined, { raw: true });
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  },
};

export async function signIn(email, password, mfaCode) {
  const payload = await request('POST', '/api/auth/login',
    { email, password, mfa_code: mfaCode || null }, { allowAnonymous: true });
  state.token = payload.access_token;
  state.user = payload.user;
  localStorage.setItem('tk_token', state.token);
  return payload.user;
}

export function signOut(silent) {
  state.token = null;
  state.user = null;
  localStorage.removeItem('tk_token');
  if (!silent) location.hash = '';
  location.reload();
}

export async function loadSession() {
  if (!state.token) return null;
  try {
    state.user = await api.get('/api/auth/me');
    return state.user;
  } catch {
    state.token = null;
    localStorage.removeItem('tk_token');
    return null;
  }
}

export function can(capability) {
  return !!state.user && state.user.capabilities.includes(capability);
}

/* ---------- Formatting (FR-A-04) ---------- */

export function durationFormat() {
  return state.user?.organisation?.duration_format || 'hm';
}

export function fmtDuration(minutes, format) {
  if (minutes === null || minutes === undefined || minutes === '') return '—';
  const fmt = format || durationFormat();
  const sign = minutes < 0 ? '-' : '';
  const value = Math.abs(Math.round(minutes));
  if (fmt === 'decimal') return `${sign}${(value / 60).toFixed(2)}`;
  return `${sign}${Math.floor(value / 60)}:${String(value % 60).padStart(2, '0')}`;
}

export function fmtSigned(minutes) {
  if (!minutes) return fmtDuration(0);
  return (minutes > 0 ? '+' : '') + fmtDuration(minutes);
}

export function fmtDate(value) {
  if (!value) return '—';
  const date = typeof value === 'string' ? new Date(value + (value.length === 10 ? 'T00:00:00' : '')) : value;
  return date.toLocaleDateString(undefined, { day: '2-digit', month: 'short', year: 'numeric' });
}

export function fmtDateShort(value) {
  if (!value) return '';
  const date = new Date(value.length === 10 ? value + 'T00:00:00' : value);
  return date.toLocaleDateString(undefined, { weekday: 'short', day: '2-digit', month: 'short' });
}

export function fmtTime(value) {
  if (!value) return '—';
  const date = new Date(value.endsWith('Z') ? value : value + 'Z');
  return date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

export function fmtDateTime(value) {
  if (!value) return '—';
  const date = new Date(value.endsWith('Z') ? value : value + 'Z');
  return date.toLocaleString(undefined, {
    day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

export function today() {
  return new Date().toISOString().slice(0, 10);
}

export function addDays(isoDate, days) {
  const date = new Date(isoDate + 'T00:00:00');
  date.setDate(date.getDate() + days);
  return date.toISOString().slice(0, 10);
}

export function startOfMonth(isoDate) {
  return isoDate.slice(0, 8) + '01';
}

/* ---------- DOM helpers ---------- */

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === 'dataset') Object.assign(node.dataset, value);
    else node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === 'string' || typeof child === 'number'
      ? document.createTextNode(String(child)) : child);
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function toast(message, kind = 'info', ms = 4200) {
  const host = document.getElementById('toasts');
  const node = el('div', { class: `toast ${kind}`, role: 'status' }, message);
  host.appendChild(node);
  setTimeout(() => node.remove(), ms);
}

export function errorToast(error) {
  const payload = error?.payload || {};
  let message = error?.message || 'Something went wrong.';
  if (payload.errors?.length) message = payload.errors.join(' ');
  if (payload.exceptions?.length) {
    message += ` (${payload.exceptions.length} blocking exception(s))`;
  }
  toast(message, 'err', 7000);
}

export function modal({ title, body, actions }) {
  const dialog = el('dialog', { 'aria-label': title });
  const foot = el('div', { class: 'dlg-foot' });
  dialog.append(
    el('div', { class: 'dlg-head' }, title),
    el('div', { class: 'dlg-body' }, body),
    foot,
  );
  for (const action of actions || []) {
    foot.appendChild(el('button', {
      class: action.class || '',
      onClick: () => action.onClick ? action.onClick(dialog) : dialog.close(),
    }, action.label));
  }
  document.body.appendChild(dialog);
  dialog.addEventListener('close', () => dialog.remove());
  dialog.showModal();
  return dialog;
}

export function confirmDialog(title, message) {
  return new Promise((resolve) => {
    const dialog = modal({
      title,
      body: el('p', {}, message),
      actions: [
        { label: 'Cancel', onClick: (d) => { d.close(); resolve(false); } },
        { label: 'Confirm', class: 'primary', onClick: (d) => { d.close(); resolve(true); } },
      ],
    });
    dialog.addEventListener('cancel', () => resolve(false));
  });
}

export function promptDialog(title, label, { required = true, textarea = true } = {}) {
  return new Promise((resolve) => {
    const input = el(textarea ? 'textarea' : 'input', { id: 'prompt-field', required });
    const error = el('div', { class: 'hint' });
    const dialog = modal({
      title,
      body: el('div', {},
        el('label', { for: 'prompt-field' }, label),
        input,
        error),
      actions: [
        { label: 'Cancel', onClick: (d) => { d.close(); resolve(null); } },
        {
          label: 'Save', class: 'primary', onClick: (d) => {
            if (required && input.value.trim().length < 3) {
              error.textContent = 'Please give at least a few words.';
              input.focus();
              return;
            }
            d.close();
            resolve(input.value.trim());
          },
        },
      ],
    });
    dialog.addEventListener('cancel', () => resolve(null));
    setTimeout(() => input.focus(), 30);
  });
}
