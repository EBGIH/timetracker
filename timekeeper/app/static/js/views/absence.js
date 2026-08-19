/* Absence: request form with live validation, my requests, team calendar and
   the approval queue (Module F). */

import {
  addDays, api, can, clear, el, errorToast, fmtDuration, promptDialog, state,
  toast, today,
} from '../api.js';

export async function renderAbsence(params) {
  const tabs = ['request', 'mine', 'calendar'];
  if (can('approve_absence')) tabs.push('approvals');
  let active = params.get('tab') || 'request';
  if (!tabs.includes(active)) active = 'request';

  const labels = {
    request: 'New request', mine: 'My requests', calendar: 'Team calendar',
    approvals: 'Awaiting my approval',
  };
  const bar = el('div', { class: 'tabs', role: 'tablist' });
  const body = el('div');
  for (const tab of tabs) {
    bar.appendChild(el('button', {
      role: 'tab', 'aria-selected': String(tab === active),
      onClick: async () => {
        bar.querySelectorAll('button').forEach((b, i) =>
          b.setAttribute('aria-selected', String(tabs[i] === tab)));
        clear(body);
        body.appendChild(el('div', { class: 'empty' }, 'Loading…'));
        const node = await panel(tab);
        clear(body);
        body.appendChild(node);
      },
    }, labels[tab]));
  }
  body.appendChild(await panel(active));
  return el('div', {}, bar, body);
}

async function panel(tab) {
  if (tab === 'mine') return myRequests();
  if (tab === 'calendar') return teamCalendar();
  if (tab === 'approvals') return approvalQueue();
  return requestForm();
}

/* ---------- Request form (US-05) ---------- */

async function requestForm() {
  const [policies, balances] = await Promise.all([
    api.get('/api/org/absence-policies'),
    api.get('/api/absence/balances'),
  ]);

  const policySelect = el('select', { id: 'policy', 'aria-label': 'Policy' },
    policies.map((p) => el('option', { value: p.id }, p.name)));
  const start = el('input', { type: 'date', value: addDays(today(), 7), id: 'a-start' });
  const end = el('input', { type: 'date', value: addDays(today(), 7), id: 'a-end' });
  const partDay = el('input', { type: 'number', step: '0.5', min: '0.5', max: '12', id: 'a-part', placeholder: 'e.g. 4' });
  const reason = el('textarea', { id: 'a-reason', placeholder: 'Optional note for your approver' });
  const document_ref = el('input', { id: 'a-doc', placeholder: 'e.g. sick note reference' });
  const feedback = el('div', { id: 'a-feedback' });

  async function preview() {
    clear(feedback);
    const body = {
      policy_id: policySelect.value,
      start_date: start.value,
      end_date: end.value,
      part_day_hours: (start.value === end.value && partDay.value) ? Number(partDay.value) : null,
    };
    try {
      const result = await api.post('/api/absence/preview', body);
      const balance = result.balance;
      feedback.appendChild(el('div', { class: 'grid cols-4' },
        stat('This request', `${result.deducted_days} day(s)`, fmtDuration(result.deducted_minutes)),
        stat('Entitlement', balance.unlimited ? '—'
          : fmtDuration(balance.accrued_minutes + balance.carried_over_minutes)),
        stat('Taken / planned', `${fmtDuration(balance.taken_minutes)} / ${fmtDuration(balance.planned_minutes)}`),
        stat('Remaining', balance.unlimited ? '—' : fmtDuration(balance.remaining_minutes),
          null, balance.remaining_minutes < 0 ? 'neg' : 'pos')));
      for (const message of result.errors) {
        feedback.appendChild(el('div', { class: 'msg err' }, message));
      }
      for (const message of result.warnings) {
        feedback.appendChild(el('div', { class: 'msg warn' }, message));
      }
      submit.disabled = result.errors.length > 0;
      feedback.appendChild(el('p', { class: 'hint' },
        'Weekends and public holidays are excluded from the deduction. '
        + 'Balance is consumed when the request is approved, not when it is raised.'));
    } catch (error) { errorToast(error); }
  }

  const submit = el('button', {
    class: 'primary big',
    onClick: async () => {
      try {
        const result = await api.post('/api/absence/requests', {
          policy_id: policySelect.value,
          start_date: start.value,
          end_date: end.value,
          part_day_hours: (start.value === end.value && partDay.value) ? Number(partDay.value) : null,
          reason: reason.value,
          document_ref: document_ref.value || null,
        });
        toast(`Request ${result.status}.`, 'ok');
        location.hash = '#/absence?tab=mine';
        location.reload();
      } catch (error) { errorToast(error); }
    },
  }, 'Send request');

  [policySelect, start, end, partDay].forEach((node) =>
    node.addEventListener('change', () => {
      if (end.value < start.value) end.value = start.value;
      preview();
    }));

  const card = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Request time off')),
    el('div', { class: 'row' },
      el('div', { class: 'field' }, el('label', { for: 'policy' }, 'Policy'), policySelect),
      el('div', { class: 'field' }, el('label', { for: 'a-start' }, 'From'), start),
      el('div', { class: 'field' }, el('label', { for: 'a-end' }, 'To'), end),
      el('div', { class: 'field' }, el('label', { for: 'a-part' }, 'Hours (part day only)'), partDay)),
    el('div', { class: 'field' }, el('label', { for: 'a-reason' }, 'Note'), reason),
    el('div', { class: 'field' }, el('label', { for: 'a-doc' }, 'Document reference'), document_ref),
    feedback,
    submit);

  await preview();

  const summary = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Your balances')),
    el('div', { class: 'grid cols-3' }, balances.map((b) =>
      stat(b.policy_name, b.unlimited ? 'no limit' : fmtDuration(b.remaining_minutes),
        b.unlimited ? 'approval required' : 'remaining'))));

  return el('div', {}, card, summary);
}

function stat(label, value, sub, tone) {
  return el('div', { class: 'stat' },
    el('div', { class: 'label' }, label),
    el('div', { class: `value ${tone || ''}` }, value),
    sub ? el('div', { class: 'sub' }, sub) : null);
}

/* ---------- My requests ---------- */

async function myRequests() {
  const rows = await api.get('/api/absence/requests?scope=self');
  const card = el('div', { class: 'card' }, el('header', {}, el('h2', {}, 'My requests')));
  if (!rows.length) {
    card.appendChild(el('div', { class: 'empty' }, 'You have not requested any absence yet.'));
    return card;
  }
  const table = el('table', {},
    el('thead', {}, el('tr', {},
      el('th', {}, 'Policy'), el('th', {}, 'From'), el('th', {}, 'To'),
      el('th', { class: 'num' }, 'Deducted'), el('th', {}, 'Status'), el('th', {}, 'Note'), el('th', {}, ''))),
    el('tbody', {}, rows.map((row) => el('tr', {},
      el('td', {}, row.policy_name),
      el('td', {}, row.start_date),
      el('td', {}, row.end_date),
      el('td', { class: 'num' }, fmtDuration(row.deducted_minutes)),
      el('td', {}, el('span', {
        class: `badge ${{ approved: 'ok', pending: 'warn', rejected: 'err', cancelled: 'mute' }[row.status] || 'info'}`,
      }, row.status)),
      el('td', { class: 'wrap' }, row.decision_note || row.reason || ''),
      el('td', {}, ['pending', 'approved'].includes(row.status) ? el('button', {
        class: 'small',
        onClick: async () => {
          const note = await promptDialog('Cancel request', 'Why?', { required: false });
          try {
            await api.post(`/api/absence/requests/${row.id}/cancel`, { note: note || '' });
            toast('Cancelled — the balance has been restored.', 'ok');
            location.reload();
          } catch (error) { errorToast(error); }
        },
      }, 'Cancel') : null)))));
  card.appendChild(el('div', { class: 'table-wrap' }, table));
  return card;
}

/* ---------- Team calendar (FR-F-05) ---------- */

async function teamCalendar() {
  const start = today().slice(0, 8) + '01';
  const endDate = addDays(start, 41);
  let data;
  try { data = await api.get(`/api/absence/calendar?start=${start}&end=${endDate}`); }
  catch (error) { return el('div', { class: 'msg err' }, error.message); }

  const days = [];
  for (let cursor = start; cursor <= endDate; cursor = addDays(cursor, 1)) days.push(cursor);

  const byUser = new Map();
  for (const entry of data.entries) {
    if (!byUser.has(entry.user_name)) byUser.set(entry.user_name, new Map());
    for (const day of entry.days) {
      byUser.get(entry.user_name).set(day, entry);
    }
  }

  const head = el('tr', {}, el('th', {}, 'Employee'),
    days.map((day) => {
      const date = new Date(day + 'T00:00:00');
      return el('th', {
        class: 'num',
        title: day,
      }, String(date.getDate()));
    }));

  const body = [...byUser.entries()].sort().map(([name, map]) => el('tr', {},
    el('th', { scope: 'row' }, name),
    days.map((day) => {
      const entry = map.get(day);
      const weekend = [0, 6].includes(new Date(day + 'T00:00:00').getDay());
      return el('td', {
        class: 'num',
        style: entry
          ? `background:${entry.status === 'approved' ? '#e6dff2' : '#f6efdc'}`
          : (weekend ? 'background:#eef2f6' : ''),
        title: entry ? `${entry.user_name}: ${entry.policy} (${entry.status})` : day,
      }, entry ? (entry.status === 'approved' ? '●' : '○') : '');
    })));

  const card = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Team calendar'),
      el('div', { class: 'spacer' }),
      el('span', { class: 'hint' }, '● approved  ○ pending')),
    byUser.size
      ? el('div', { class: 'table-wrap' },
        el('table', {}, el('thead', {}, head), el('tbody', {}, body)))
      : el('div', { class: 'empty' }, 'No absence in this window.'));
  return card;
}

/* ---------- Approval queue ---------- */

async function approvalQueue() {
  const rows = await api.get('/api/absence/requests?scope=team&status_filter=pending');
  const card = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Absence awaiting your approval'),
      el('span', { class: 'badge warn' }, String(rows.length))));
  if (!rows.length) {
    card.appendChild(el('div', { class: 'empty' }, 'Nothing waiting.'));
    return card;
  }
  const table = el('table', {},
    el('thead', {}, el('tr', {},
      el('th', {}, 'Employee'), el('th', {}, 'Policy'), el('th', {}, 'From'),
      el('th', {}, 'To'), el('th', { class: 'num' }, 'Deducted'), el('th', {}, 'Note'), el('th', {}, ''))),
    el('tbody', {}, rows.filter((r) => r.user_id !== state.user.id).map((row) => el('tr', {},
      el('td', {}, row.user_name),
      el('td', {}, row.policy_name),
      el('td', {}, row.start_date),
      el('td', {}, row.end_date),
      el('td', { class: 'num' }, fmtDuration(row.deducted_minutes)),
      el('td', { class: 'wrap' }, row.reason || ''),
      el('td', {},
        el('button', {
          class: 'small primary',
          onClick: async () => {
            try {
              const result = await api.post(`/api/absence/requests/${row.id}/approve`, { note: '' });
              toast(`Request ${result.status}.`
                + (result.warnings?.length ? ` ${result.warnings.join(' ')}` : ''), 'ok');
              location.reload();
            } catch (error) { errorToast(error); }
          },
        }, 'Approve'),
        el('button', {
          class: 'small',
          onClick: async () => {
            const note = await promptDialog('Reject request', 'Reason (shown to the employee)');
            if (!note) return;
            try {
              await api.post(`/api/absence/requests/${row.id}/reject`, { note });
              toast('Rejected.', 'ok');
              location.reload();
            } catch (error) { errorToast(error); }
          },
        }, 'Reject'))))));
  card.appendChild(el('div', { class: 'table-wrap' }, table));
  return card;
}
