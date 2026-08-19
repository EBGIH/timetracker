/* Approval queue, period locking, corrections and overtime (Module H). */

import {
  api, can, clear, confirmDialog, el, errorToast, fmtDuration, fmtSigned,
  promptDialog, toast,
} from '../api.js';

export async function renderApprovals(params) {
  const wrap = el('div');
  const tabs = ['queue', 'corrections', 'overtime'];
  let active = params.get('tab') || 'queue';
  if (!tabs.includes(active)) active = 'queue';

  const bar = el('div', { class: 'tabs', role: 'tablist' });
  const body = el('div');
  const labels = { queue: 'Timesheets', corrections: 'Corrections', overtime: 'Overtime' };
  for (const tab of tabs) {
    bar.appendChild(el('button', {
      role: 'tab', 'aria-selected': String(tab === active),
      onClick: async () => {
        active = tab;
        bar.querySelectorAll('button').forEach((b, i) =>
          b.setAttribute('aria-selected', String(tabs[i] === tab)));
        clear(body);
        body.appendChild(await panel(tab));
      },
    }, labels[tab]));
  }
  wrap.append(bar, body);
  body.appendChild(await panel(active));
  return wrap;
}

async function panel(tab) {
  if (tab === 'corrections') return correctionsPanel();
  if (tab === 'overtime') return overtimePanel();
  return queuePanel();
}

/* ---------- Timesheet approval queue (US-04) ---------- */

async function queuePanel() {
  const periods = await api.get('/api/periods?count=8');
  const container = el('div');
  const select = el('select', { 'aria-label': 'Period' },
    periods.map((p) => el('option', { value: p.id },
      `${p.start_date} – ${p.end_date}${p.status === 'locked' ? ' (locked)' : ''}`)));
  const target = el('div');

  const header = el('div', { class: 'card' },
    el('header', {},
      el('h2', {}, 'Approval queue'),
      el('div', { class: 'spacer' }),
      select,
      el('button', { class: 'primary', onClick: () => load(select.value) }, 'Load')));

  container.append(header, target);
  await load(select.value);
  return container;

  async function load(periodId) {
    clear(target);
    target.appendChild(el('div', { class: 'empty' }, 'Loading…'));
    let data;
    try { data = await api.get(`/api/approvals/queue?period_id=${periodId}`); }
    catch (error) { clear(target); target.appendChild(el('div', { class: 'msg err' }, error.message)); return; }
    clear(target);

    const selected = new Set();
    const checkAll = el('input', {
      type: 'checkbox', 'aria-label': 'Select all approvable',
      onChange: (event) => {
        target.querySelectorAll('input[data-user]').forEach((box) => {
          if (box.disabled) return;
          box.checked = event.target.checked;
          if (box.checked) selected.add(box.dataset.user); else selected.delete(box.dataset.user);
        });
        updateButtons();
      },
    });

    const rows = data.rows.map((row) => {
      const box = el('input', {
        type: 'checkbox', dataset: { user: row.user_id },
        disabled: !row.can_approve,
        'aria-label': `Select ${row.name}`,
        onChange: (event) => {
          if (event.target.checked) selected.add(row.user_id); else selected.delete(row.user_id);
          updateButtons();
        },
      });
      return el('tr', {},
        el('td', {}, box),
        el('td', {}, row.name, el('div', { class: 'hint' }, row.personnel_number)),
        el('td', {}, el('span', {
          class: `badge ${row.status === 'approved' ? 'ok' : row.status === 'submitted' ? 'warn' : 'mute'}`,
        }, row.excluded ? 'excluded' : row.status)),
        el('td', { class: 'num' }, fmtDuration(row.worked_minutes)),
        el('td', { class: 'num' }, fmtDuration(row.expected_minutes)),
        el('td', { class: 'num' }, fmtSigned(row.difference_minutes)),
        el('td', { class: 'num' }, fmtDuration(row.overtime_minutes)),
        el('td', { class: 'num' },
          row.blocking_count
            ? el('span', { class: 'badge err' }, `${row.blocking_count} blocking`)
            : row.exception_count
              ? el('span', { class: 'badge warn' }, String(row.exception_count))
              : el('span', { class: 'badge ok' }, '0')),
        el('td', {},
          el('a', { class: 'btn small', href: `#/reports?user=${row.user_id}` }, 'Detail'),
          el('button', {
            class: 'small',
            onClick: async () => {
              const reason = await promptDialog('Exclude from this period',
                'Why is this employee excluded? A period can only be locked when '
                + 'everyone is approved or explicitly excluded (BR-09).');
              if (!reason) return;
              try {
                await api.post('/api/approvals/exclude',
                  { period_id: periodId, user_id: row.user_id, reason });
                toast('Excluded.', 'ok');
                load(periodId);
              } catch (error) { errorToast(error); }
            },
          }, 'Exclude')));
    });

    const approveButton = el('button', {
      class: 'primary', disabled: true,
      onClick: async () => {
        try {
          const result = await api.post('/api/approvals/approve',
            { period_id: periodId, user_ids: [...selected] });
          toast(`Approved ${result.approved.length}.`
            + (result.skipped.length ? ` ${result.skipped.length} skipped.` : ''), 'ok');
          load(periodId);
        } catch (error) { errorToast(error); }
      },
    }, 'Approve selected');

    const rejectButton = el('button', {
      disabled: true,
      onClick: async () => {
        const reason = await promptDialog('Reject period',
          'A reason is mandatory and is shown to the employee.');
        if (!reason) return;
        try {
          await api.post('/api/approvals/reject',
            { period_id: periodId, user_ids: [...selected], reason });
          toast('Returned to the employee(s).', 'ok');
          load(periodId);
        } catch (error) { errorToast(error); }
      },
    }, 'Reject selected');

    function updateButtons() {
      approveButton.disabled = selected.size === 0;
      rejectButton.disabled = selected.size === 0;
    }

    const lockControls = can('lock_period') ? el('div', { class: 'row' },
      el('button', {
        onClick: async () => {
          const reason = await promptDialog('Lock period',
            'Note for the audit log (optional but recommended).', { required: false });
          try {
            await api.post('/api/approvals/lock', { period_id: periodId, reason: reason || '' });
            toast('Period locked.', 'ok');
            load(periodId);
          } catch (error) {
            const outstanding = error.payload?.outstanding || [];
            if (outstanding.length) {
              const { modal } = await import('../api.js');
              modal({
                title: 'Not everyone is approved',
                body: el('div', {},
                  el('p', {}, error.payload.message),
                  el('ul', {}, outstanding.slice(0, 30).map((item) =>
                    el('li', {}, `${item.name} — ${item.status}`)))),
                actions: [{ label: 'Close', class: 'primary' }],
              });
            } else errorToast(error);
          }
        },
      }, 'Lock period'),
      el('button', {
        onClick: async () => {
          const reason = await promptDialog('Unlock period',
            'Unlocking is audited. Why is it necessary?');
          if (!reason) return;
          try {
            await api.post('/api/approvals/unlock', { period_id: periodId, reason });
            toast('Period unlocked.', 'ok');
            load(periodId);
          } catch (error) { errorToast(error); }
        },
      }, 'Unlock period'),
      can('payroll_export') ? el('a', { class: 'btn', href: '#/reports?tab=payroll' }, 'Payroll export') : null,
    ) : null;

    target.appendChild(el('div', { class: 'card' },
      el('header', {},
        el('h2', {}, `${data.period.start_date} – ${data.period.end_date}`),
        el('span', { class: `badge ${data.period.status === 'locked' ? 'mute' : 'info'}` },
          data.period.status),
        el('div', { class: 'spacer' }),
        approveButton, rejectButton),
      el('div', { class: 'table-wrap' },
        el('table', {},
          el('thead', {}, el('tr', {},
            el('th', {}, checkAll), el('th', {}, 'Employee'), el('th', {}, 'Status'),
            el('th', { class: 'num' }, 'Worked'), el('th', { class: 'num' }, 'Expected'),
            el('th', { class: 'num' }, 'Difference'), el('th', { class: 'num' }, 'Overtime'),
            el('th', { class: 'num' }, 'Exceptions'), el('th', {}, ''))),
          el('tbody', {}, rows))),
      lockControls));
  }
}

/* ---------- Corrections (FR-H-06) ---------- */

async function correctionsPanel() {
  const rows = await api.get('/api/corrections?status_filter=pending');
  const card = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Correction requests'),
      el('span', { class: 'badge warn' }, String(rows.length))));
  if (!rows.length) {
    card.appendChild(el('div', { class: 'empty' }, 'No correction requests waiting.'));
    return card;
  }
  const table = el('table', {},
    el('thead', {}, el('tr', {},
      el('th', {}, 'Employee'), el('th', {}, 'Day'), el('th', {}, 'Record'),
      el('th', {}, 'Proposed'), el('th', {}, 'Reason'), el('th', {}, ''))),
    el('tbody', {}, rows.map((row) => el('tr', {},
      el('td', {}, row.user_name),
      el('td', {}, row.day),
      el('td', {}, row.entity_type.replace('_', ' ')),
      el('td', { class: 'wrap' }, JSON.stringify(row.proposed)),
      el('td', { class: 'wrap' }, row.reason),
      el('td', {},
        el('button', {
          class: 'small primary',
          onClick: async () => {
            if (!await confirmDialog('Approve correction',
              'A new version will supersede the current record. The original is kept.')) return;
            try {
              await api.post(`/api/corrections/${row.id}/approve`, { note: '' });
              toast('Correction applied.', 'ok');
              location.reload();
            } catch (error) { errorToast(error); }
          },
        }, 'Approve'),
        el('button', {
          class: 'small',
          onClick: async () => {
            const note = await promptDialog('Reject correction', 'Reason');
            if (!note) return;
            try {
              await api.post(`/api/corrections/${row.id}/reject`, { note });
              toast('Rejected.', 'ok');
              location.reload();
            } catch (error) { errorToast(error); }
          },
        }, 'Reject'))))));
  card.appendChild(el('div', { class: 'table-wrap' }, table));
  return card;
}

/* ---------- Overtime approval (FR-G-04) ---------- */

async function overtimePanel() {
  const rows = await api.get('/api/overtime/requests?status_filter=pending');
  const card = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Overtime awaiting approval'),
      el('span', { class: 'badge warn' }, String(rows.length))),
    el('p', { class: 'hint' },
      'Where the organisation requires prior approval, unapproved overtime is '
      + 'recorded and reported but excluded from the payroll export.'));
  if (!rows.length) {
    card.appendChild(el('div', { class: 'empty' }, 'Nothing waiting.'));
    return card;
  }
  const table = el('table', {},
    el('thead', {}, el('tr', {},
      el('th', {}, 'Employee'), el('th', {}, 'Day'), el('th', { class: 'num' }, 'Minutes'),
      el('th', {}, 'Reason'), el('th', {}, ''))),
    el('tbody', {}, rows.map((row) => el('tr', {},
      el('td', {}, row.user_name),
      el('td', {}, row.day),
      el('td', { class: 'num' }, fmtDuration(row.minutes)),
      el('td', { class: 'wrap' }, row.reason),
      el('td', {},
        ...['approved', 'rejected'].map((decision) => el('button', {
          class: `small ${decision === 'approved' ? 'primary' : ''}`,
          onClick: async () => {
            try {
              await api.post(`/api/overtime/requests/${row.id}/decide`, { decision });
              toast(`Overtime ${decision}.`, 'ok');
              location.reload();
            } catch (error) { errorToast(error); }
          },
        }, decision === 'approved' ? 'Approve' : 'Reject')))))));
  card.appendChild(el('div', { class: 'table-wrap' }, table));
  return card;
}
