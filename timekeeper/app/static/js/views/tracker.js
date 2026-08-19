/* Tracker — the primary employee surface (specification section 18).
   A persistent entry row with a timer/manual toggle, then entries grouped by
   day with a per-day total and the period total in the header. */

import {
  addDays, api, clear, confirmDialog, el, errorToast, fmtDuration, fmtSigned,
  fmtTime, modal, promptDialog, state, toast, today,
} from '../api.js';

let tick = null;
let mode = localStorage.getItem('tk_mode') || 'timer';

function toIsoUtc(dateStr, timeStr) {
  if (!dateStr || !timeStr) return null;
  return new Date(`${dateStr}T${timeStr}`).toISOString();
}

function localTimeValue(iso) {
  const date = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
}

function localDateValue(iso) {
  const date = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

export async function renderTracker(params) {
  const on = params.get('date') || today();
  const [data, centres, breakTypes] = await Promise.all([
    api.get(`/api/attendance/tracker?on=${on}&days=14`),
    api.get('/api/org/cost-centres').catch(() => []),
    api.get('/api/org/break-types').catch(() => []),
  ]);

  const wrap = el('div');
  wrap.append(
    periodHeader(data),
    entryBar(data, centres, breakTypes, on),
    exceptionPanel(data),
    dayList(data, centres),
    gridCard(on),
  );

  if (tick) clearInterval(tick);
  if (data.running) {
    tick = setInterval(() => {
      const node = document.getElementById('elapsed');
      if (!node) { clearInterval(tick); return; }
      const started = new Date(data.running.start_at + 'Z');
      const seconds = Math.max(0, Math.floor((Date.now() - started.getTime()) / 1000));
      node.textContent = [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60]
        .map((v) => String(v).padStart(2, '0')).join(':');
    }, 1000);
  }

  installShortcuts(data);
  return wrap;
}

/* ---------- Period header ---------- */

function periodHeader(data) {
  const period = data.period;
  const totals = period.totals;
  const statusBadge = {
    open: 'info', submitted: 'warn', approved: 'ok', locked: 'mute',
  }[period.approval_status === 'open' ? period.status : period.approval_status] || 'info';
  const label = period.status === 'locked' ? 'locked' : period.approval_status;

  const submit = el('button', {
    class: 'primary',
    disabled: period.status === 'locked' || period.approval_status !== 'open',
    onClick: async () => {
      try {
        await api.post('/api/approvals/submit', { period_id: period.id });
        toast('Period submitted for approval.', 'ok');
        location.reload();
      } catch (error) {
        const blocking = error.payload?.exceptions || [];
        if (blocking.length) {
          modal({
            title: 'Resolve these first',
            body: el('div', {},
              el('p', {}, 'The period cannot be submitted while these remain open:'),
              el('ul', {}, blocking.map((item) =>
                el('li', {}, `${item.day} — ${item.type.replace(/_/g, ' ').toLowerCase()}: ${item.detail}`)))),
            actions: [{ label: 'Close', class: 'primary' }],
          });
        } else errorToast(error);
      }
    },
  }, 'Submit period');

  return el('div', { class: 'card' },
    el('header', {},
      el('h2', {}, `Period ${period.start_date} – ${period.end_date}`),
      el('span', { class: `badge ${statusBadge}` }, label),
      el('div', { class: 'spacer' }),
      period.cutoff_date ? el('span', { class: 'hint' }, `Cut-off ${period.cutoff_date}`) : null,
      submit),
    el('div', { class: 'grid cols-4' },
      stat('Worked', fmtDuration(totals.net_worked_minutes)),
      stat('Expected', fmtDuration(totals.expected_minutes)),
      stat('Balance', fmtSigned(totals.balance_minutes),
        totals.balance_minutes < 0 ? 'neg' : 'pos'),
      stat('Overtime', fmtDuration(totals.overtime_total)),
    ));
}

function stat(label, value, tone) {
  return el('div', { class: 'stat' },
    el('div', { class: 'label' }, label),
    el('div', { class: `value ${tone || ''}` }, value));
}

/* ---------- Entry bar (FR-C-01, FR-C-03) ---------- */

function entryBar(data, centres, breakTypes, on) {
  const org = state.user.organisation;
  const description = el('input', {
    id: 'entry-desc', class: 'desc', placeholder: 'What are you working on? (optional)',
    'aria-label': 'Description', value: data.running?.description || '',
  });
  const centre = el('select', { id: 'entry-cc', 'aria-label': 'Cost centre' },
    el('option', { value: '' }, org.require_cost_centre ? 'Select cost centre…' : 'No cost centre'),
    centres.map((c) => el('option', { value: c.id, selected: data.running?.cost_centre_id === c.id },
      `${c.code} — ${c.name}`)));

  const bar = el('div', { class: 'tracker-bar' }, description, centre);

  const toggle = el('div', { class: 'mode-toggle', role: 'group', 'aria-label': 'Entry mode' },
    el('button', {
      'aria-pressed': String(mode === 'timer'), onClick: () => setMode('timer'),
      disabled: !org.channels.timer,
    }, 'Timer'),
    el('button', {
      'aria-pressed': String(mode === 'manual'), onClick: () => setMode('manual'),
      disabled: !org.channels.manual,
    }, 'Manual'));

  if (mode === 'timer' || data.running) {
    const running = !!data.running;
    const elapsed = el('div', { class: 'timer-display', id: 'elapsed', 'aria-live': 'off' }, '00:00:00');
    const onBreak = running && data.running.breaks.some((b) => !b.end_at);

    bar.append(...[
      elapsed,
      el('button', {
        class: running ? 'danger big' : 'primary big', id: 'start-stop',
        onClick: () => (running ? stopTimer() : startTimer(description.value, centre.value)),
      }, running ? 'Stop' : 'Start'),
      running && breakTypes.length ? breakControl(breakTypes, onBreak) : null,
      toggle,
    ].filter(Boolean));
    if (running) {
      bar.append(el('div', { class: 'hint', style: 'flex-basis:100%' },
        `Started ${fmtTime(data.running.start_at)} · this timer is stored on the server, `
        + 'so it survives a reload, a browser close or a change of device.'));
    }
  } else {
    const dateInput = el('input', { id: 'm-date', type: 'date', value: on, 'aria-label': 'Date' });
    const from = el('input', { id: 'm-from', type: 'time', value: '09:00', 'aria-label': 'From' });
    const to = el('input', { id: 'm-to', type: 'time', value: '17:00', 'aria-label': 'To' });
    bar.append(dateInput, from, to,
      el('button', {
        class: 'primary big',
        onClick: async () => {
          try {
            await api.post('/api/attendance/sessions', {
              start_at: toIsoUtc(dateInput.value, from.value),
              end_at: toIsoUtc(dateInput.value, to.value),
              description: description.value,
              cost_centre_id: centre.value || null,
              source: 'manual',
            });
            toast('Entry added.', 'ok');
            location.reload();
          } catch (error) { handleOverlap(error); }
        },
      }, 'Add'),
      toggle);
  }
  return bar;
}

function breakControl(breakTypes, onBreak) {
  if (onBreak) {
    return el('button', {
      onClick: async () => {
        try {
          await api.post('/api/attendance/breaks/stop', {});
          toast('Break ended.', 'ok');
          location.reload();
        } catch (error) { errorToast(error); }
      },
    }, 'End break');
  }
  const select = el('select', { 'aria-label': 'Break type' },
    breakTypes.map((b) => el('option', { value: b.id },
      `${b.name}${b.is_paid ? ' · paid' : ' · unpaid'}`)));
  return el('div', { class: 'row', style: 'gap:.3rem' }, select,
    el('button', {
      onClick: async () => {
        try {
          await api.post('/api/attendance/breaks/start', { break_type_id: select.value });
          toast('Break started.', 'ok');
          location.reload();
        } catch (error) { errorToast(error); }
      },
    }, 'Start break'));
}

function setMode(next) {
  mode = next;
  localStorage.setItem('tk_mode', next);
  location.reload();
}

async function startTimer(description, costCentreId) {
  try {
    await api.post('/api/attendance/start', {
      description, cost_centre_id: costCentreId || null, source: 'timer',
    });
    location.reload();
  } catch (error) { handleOverlap(error); }
}

async function stopTimer() {
  try {
    await api.post('/api/attendance/stop', {});
    toast('Timer stopped.', 'ok');
    location.reload();
  } catch (error) { errorToast(error); }
}

function handleOverlap(error) {
  const payload = error.payload || {};
  if (payload.error === 'overlap' || payload.error === 'timer_running') {
    modal({
      title: payload.error === 'overlap' ? 'That would overlap' : 'A timer is already running',
      body: el('div', {},
        el('p', {}, payload.message),
        payload.conflict ? el('p', { class: 'hint' },
          `Conflicting entry: ${fmtTime(payload.conflict.start_at)} – `
          + `${payload.conflict.end_at ? fmtTime(payload.conflict.end_at) : 'running'}`) : null),
      actions: [
        payload.error === 'timer_running' ? {
          label: 'Stop the running timer', class: 'primary',
          onClick: (d) => { d.close(); stopTimer(); },
        } : null,
        { label: 'Close' },
      ].filter(Boolean),
    });
    return;
  }
  errorToast(error);
}

/* ---------- Exceptions ---------- */

function exceptionPanel(data) {
  if (!data.exceptions.length) return el('div');
  const list = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Needs your attention'),
      el('span', { class: 'badge warn' }, String(data.exceptions.length))));
  for (const item of data.exceptions) {
    list.appendChild(el('div', { class: 'entry' },
      el('span', { class: `badge ${item.blocking ? 'err' : 'warn'}` },
        item.blocking ? 'blocking' : 'warning'),
      el('span', { class: 'times' }, item.day),
      el('span', { class: 'grow' }, `${item.type.replace(/_/g, ' ').toLowerCase()} — ${item.detail}`),
      el('button', {
        class: 'small',
        onClick: async () => {
          const note = await promptDialog('Resolve exception',
            'What did you do about it? This is kept in the record.');
          if (!note) return;
          try {
            await api.post(`/api/attendance/exceptions/${item.id}/resolve`, { note });
            toast('Resolved.', 'ok');
            location.reload();
          } catch (error) { errorToast(error); }
        },
      }, 'Resolve')));
  }
  return list;
}

/* ---------- Day list (FR-C-07) ---------- */

function dayList(data, centres) {
  const centreName = Object.fromEntries(centres.map((c) => [c.id, `${c.code}`]));
  const wrap = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Recent activity')));

  for (const day of data.days) {
    if (!day.sessions.length && !day.totals.expected && !day.totals.absence) continue;
    const totals = day.totals || {};
    wrap.appendChild(el('div', { class: 'day-group' },
      el('header', {},
        el('span', {}, new Date(day.day + 'T00:00:00').toLocaleDateString(undefined,
          { weekday: 'long', day: '2-digit', month: 'long' })),
        el('span', {},
          totals.is_holiday ? el('span', { class: 'badge info' }, 'public holiday') : '',
          ' ',
          el('span', { class: 'dur' }, fmtDuration(totals.net || 0)),
          totals.expected ? el('span', { class: 'hint' }, ` / ${fmtDuration(totals.expected)}`) : '')),
      day.sessions.length
        ? day.sessions.map((session) => entryRow(session, centreName))
        : el('div', { class: 'entry' }, el('span', { class: 'hint grow' },
          totals.absence ? 'Approved absence' : 'No attendance recorded'))));
  }
  return wrap;
}

function entryRow(session, centreName) {
  const row = el('div', { class: `entry ${session.running ? 'running' : ''}` });
  row.append(
    el('span', { class: 'grow' },
      session.description || el('span', { class: 'hint' }, 'No description'),
      session.cost_centre_id ? el('span', { class: 'badge mute', style: 'margin-left:.4rem' },
        centreName[session.cost_centre_id] || 'CC') : null,
      session.source !== 'timer' ? el('span', { class: 'badge info', style: 'margin-left:.4rem' },
        session.source) : null,
      session.recorded_by_other ? el('span', { class: 'badge warn', style: 'margin-left:.4rem' },
        'recorded by someone else') : null,
      session.system_generated && !session.confirmed
        ? el('span', { class: 'badge err', style: 'margin-left:.4rem' }, 'auto-stopped — confirm') : null),
    el('span', { class: 'times' },
      `${fmtTime(session.start_at)} – ${session.end_at ? fmtTime(session.end_at) : '…'}`),
    session.breaks.length ? el('span', { class: 'hint' },
      `${session.breaks.length} break(s), ${fmtDuration(session.breaks.reduce((a, b) => a + b.minutes, 0))}`) : null,
    el('span', { class: 'dur' }, fmtDuration(session.net_minutes)),
    el('button', { class: 'small', onClick: () => editEntry(session) }, 'Edit'),
    !session.running ? el('button', {
      class: 'small', title: 'Start a new entry with the same description',
      onClick: async () => {
        try {
          await api.post(`/api/attendance/sessions/${session.id}/continue`);
          location.reload();
        } catch (error) { handleOverlap(error); }
      },
    }, 'Continue') : null,
    !session.running ? el('button', {
      class: 'small',
      onClick: async () => {
        try {
          await api.post(`/api/attendance/sessions/${session.id}/duplicate`);
          toast('Duplicated.', 'ok');
          location.reload();
        } catch (error) { handleOverlap(error); }
      },
    }, 'Duplicate') : null,
    el('button', {
      class: 'small', onClick: async () => {
        if (!await confirmDialog('Delete entry', 'This entry will be removed. The deletion is recorded in the audit log.')) return;
        try {
          await api.del(`/api/attendance/sessions/${session.id}`);
          toast('Deleted.', 'ok');
          location.reload();
        } catch (error) { handleCorrection(error, session); }
      },
    }, 'Delete'),
  );
  return row;
}

function editEntry(session) {
  const dateValue = localDateValue(session.start_at);
  const startValue = localTimeValue(session.start_at);
  const endValue = session.end_at ? localTimeValue(session.end_at) : '';
  const fields = {
    date: el('input', { type: 'date', value: dateValue }),
    from: el('input', { type: 'time', value: startValue }),
    to: el('input', { type: 'time', value: endValue }),
    description: el('input', { value: session.description || '' }),
    reason: el('textarea', { placeholder: 'Why is this being changed?' }),
  };
  const body = el('div', {},
    el('div', { class: 'row' },
      el('div', { class: 'field' }, el('label', {}, 'Date'), fields.date),
      el('div', { class: 'field' }, el('label', {}, 'From'), fields.from),
      el('div', { class: 'field' }, el('label', {}, 'To'), fields.to)),
    el('div', { class: 'field' }, el('label', {}, 'Description'), fields.description),
    el('div', { class: 'field' }, el('label', {}, 'Reason for the change'), fields.reason),
    session.system_generated && !session.confirmed
      ? el('div', { class: 'msg warn' },
        'This clock-out was generated by the system because the timer ran past the '
        + 'configured maximum. Confirm it or correct the end time.') : null,
    el('p', { class: 'hint' },
      'The original values are kept in the audit trail; nothing is overwritten.'),
  );
  modal({
    title: 'Edit entry',
    body,
    actions: [
      { label: 'Cancel' },
      session.system_generated && !session.confirmed ? {
        label: 'Confirm as is', onClick: async (d) => {
          try {
            await api.put(`/api/attendance/sessions/${session.id}`,
              { confirm: true, reason: 'Confirmed by employee' });
            d.close(); location.reload();
          } catch (error) { errorToast(error); }
        },
      } : null,
      {
        label: 'Save', class: 'primary', onClick: async (d) => {
          try {
            await api.put(`/api/attendance/sessions/${session.id}`, {
              start_at: toIsoUtc(fields.date.value, fields.from.value),
              end_at: fields.to.value ? toIsoUtc(fields.date.value, fields.to.value) : null,
              description: fields.description.value,
              reason: fields.reason.value,
              confirm: true,
            });
            d.close();
            toast('Saved.', 'ok');
            location.reload();
          } catch (error) { d.close(); handleCorrection(error, session, fields); }
        },
      },
    ].filter(Boolean),
  });
}

/* US-03 AC-3: in a locked period, offer a correction request instead. */
function handleCorrection(error, session, fields) {
  const payload = error.payload || {};
  if (!String(payload.error || '').startsWith('period_')) { errorToast(error); return; }
  const reason = el('textarea', { placeholder: 'Why does this need to change?' });
  modal({
    title: 'This period is closed',
    body: el('div', {},
      el('p', {}, payload.message),
      el('div', { class: 'field' }, el('label', {}, 'Reason'), reason)),
    actions: [
      { label: 'Cancel' },
      {
        label: 'Request correction', class: 'primary', onClick: async (d) => {
          const proposed = {};
          if (fields) {
            proposed.start_at = toIsoUtc(fields.date.value, fields.from.value)?.replace('Z', '');
            if (fields.to.value) proposed.end_at = toIsoUtc(fields.date.value, fields.to.value)?.replace('Z', '');
            proposed.description = fields.description.value;
          }
          try {
            await api.post('/api/corrections', {
              entity_type: 'attendance_session', entity_id: session.id,
              proposed, reason: reason.value,
            });
            d.close();
            toast('Correction request sent to your manager.', 'ok');
          } catch (inner) { errorToast(inner); }
        },
      },
    ],
  });
}

/* ---------- Weekly grid (FR-C-10) ---------- */

function gridCard(on) {
  const container = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Weekly timesheet grid'),
      el('div', { class: 'spacer' }),
      el('button', { class: 'small', onClick: () => loadGrid(container, on) }, 'Load')),
    el('p', { class: 'hint' },
      'Rows are cost centres, columns are days. Use this if you prefer to fill '
      + 'the week in retrospectively.'));
  return container;
}

async function loadGrid(container, on) {
  let data;
  try { data = await api.get(`/api/attendance/grid?week_of=${on}`); }
  catch (error) { errorToast(error); return; }

  const existing = container.querySelector('.grid-table');
  if (existing) existing.remove();

  const inputs = [];
  const rows = data.cost_centres.map((centre) => {
    const cells = data.days.map((day) => {
      const current = data.rows.find((r) => r.cost_centre_id === centre.id);
      const minutes = current ? (current.cells[day] || 0) : 0;
      const input = el('input', {
        type: 'number', min: '0', max: '1440', step: '5', value: String(minutes),
        style: 'width:5.5rem', 'aria-label': `${centre.code} ${day}`,
      });
      inputs.push({ day, cost_centre_id: centre.id, input });
      return el('td', {}, input);
    });
    return el('tr', {}, el('th', { scope: 'row' }, `${centre.code} — ${centre.name}`), cells);
  });

  const table = el('div', { class: 'table-wrap grid-table' },
    el('table', {},
      el('thead', {}, el('tr', {}, el('th', {}, 'Cost centre'),
        data.days.map((d) => el('th', { class: 'num' },
          new Date(d + 'T00:00:00').toLocaleDateString(undefined, { weekday: 'short', day: '2-digit' }))))),
      el('tbody', {}, rows),
      el('tfoot', {}, el('tr', {}, el('td', {}, 'Total'),
        data.days.map((d) => el('td', { class: 'num' }, fmtDuration(data.column_totals[d] || 0)))))));

  container.append(table, el('div', { class: 'row', style: 'margin-top:.6rem' },
    el('button', {
      class: 'primary', onClick: async () => {
        const cells = inputs.map(({ day, cost_centre_id, input }) => ({
          day, cost_centre_id, minutes: Number(input.value || 0), description: '',
        }));
        try {
          await api.post('/api/attendance/grid', { cells });
          toast('Grid saved.', 'ok');
        } catch (error) { errorToast(error); }
      },
    }, 'Save grid')));
}

/* ---------- Keyboard shortcuts (FR-C-11) ---------- */

function installShortcuts(data) {
  document.onkeydown = (event) => {
    if (event.target.matches('input, textarea, select')) return;
    if (event.key.toLowerCase() === 's' && !event.metaKey && !event.ctrlKey) {
      event.preventDefault();
      document.getElementById('start-stop')?.click();
    }
    if (event.key.toLowerCase() === 'n') {
      event.preventDefault();
      document.getElementById('entry-desc')?.focus();
    }
    if (event.key.toLowerCase() === 'm') {
      event.preventDefault();
      setMode(mode === 'timer' ? 'manual' : 'timer');
    }
  };
}
