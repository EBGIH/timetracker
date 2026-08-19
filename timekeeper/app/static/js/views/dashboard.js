/* Employee and manager dashboards (specification section 18). */

import {
  api, el, errorToast, fmtDuration, fmtSigned, promptDialog, state, toast,
} from '../api.js';

const STATUS_LABEL = {
  in: 'Clocked in', on_break: 'On break', absent: 'Absent',
  expected: 'Expected, not present', finished: 'Finished', off: 'Not scheduled',
};

export async function renderMyDashboard() {
  const data = await api.get('/api/dashboard/me');
  const totals = data.totals;
  const wrap = el('div');

  wrap.append(el('div', { class: 'grid cols-4' },
    stat('This period worked', fmtDuration(totals.net_worked_minutes),
      `${data.period.start_date} – ${data.period.end_date}`),
    stat('Expected', fmtDuration(totals.expected_minutes)),
    stat('Balance', fmtSigned(totals.balance_minutes), null,
      totals.balance_minutes < 0 ? 'neg' : 'pos'),
    stat('Time bank', fmtDuration(data.time_bank_minutes), 'Compensatory time available'),
  ));

  const periodCard = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Current period'),
      el('span', { class: `badge ${data.period.approval_status === 'approved' ? 'ok'
        : data.period.approval_status === 'submitted' ? 'warn' : 'info'}` },
        data.period.status === 'locked' ? 'locked' : data.period.approval_status),
      el('div', { class: 'spacer' }),
      el('a', { class: 'btn', href: '#/tracker' }, 'Open tracker')),
    el('p', { class: 'hint' },
      data.period.days_to_cutoff === null ? ''
        : data.period.days_to_cutoff >= 0
          ? `Submission cut-off in ${data.period.days_to_cutoff} day(s), on ${data.period.cutoff_date}.`
          : `Submission was due on ${data.period.cutoff_date}.`),
    el('div', { class: 'grid cols-3' },
      stat('Overtime', fmtDuration(totals.overtime_total)),
      stat('Absence', fmtDuration(totals.absence_minutes)),
      stat('Night hours', fmtDuration(totals.night_minutes))));
  wrap.appendChild(periodCard);

  /* Balances (FR-F-06) */
  const balances = el('div', { class: 'card' }, el('header', {}, el('h2', {}, 'Absence balances')));
  const table = el('table', {},
    el('thead', {}, el('tr', {},
      el('th', {}, 'Policy'), el('th', { class: 'num' }, 'Entitlement'),
      el('th', { class: 'num' }, 'Taken'), el('th', { class: 'num' }, 'Planned'),
      el('th', { class: 'num' }, 'Pending'), el('th', { class: 'num' }, 'Remaining'))),
    el('tbody', {}, data.balances.map((balance) => el('tr', {},
      el('td', {}, balance.policy_name,
        balance.is_paid ? null : el('span', { class: 'badge mute', style: 'margin-left:.4rem' }, 'unpaid')),
      el('td', { class: 'num' }, balance.unlimited ? '—'
        : fmtDuration(balance.accrued_minutes + balance.carried_over_minutes)),
      el('td', { class: 'num' }, fmtDuration(balance.taken_minutes)),
      el('td', { class: 'num' }, fmtDuration(balance.planned_minutes)),
      el('td', { class: 'num' }, fmtDuration(balance.pending_minutes)),
      el('td', { class: 'num' }, balance.unlimited ? '—' : fmtDuration(balance.remaining_minutes))))));
  balances.appendChild(el('div', { class: 'table-wrap' }, table));
  wrap.appendChild(balances);

  /* Open exceptions */
  const exceptions = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Open items'),
      el('span', { class: data.exceptions.length ? 'badge warn' : 'badge ok' },
        `${data.exceptions.length}`)));
  if (!data.exceptions.length) {
    exceptions.appendChild(el('div', { class: 'empty' }, 'Nothing needs your attention.'));
  } else {
    for (const item of data.exceptions) {
      exceptions.appendChild(el('div', { class: 'entry' },
        el('span', { class: `badge ${item.blocking ? 'err' : 'warn'}` },
          item.blocking ? 'blocking' : 'warning'),
        el('span', { class: 'times' }, item.day),
        el('span', { class: 'grow' }, item.detail || item.type),
        el('a', { class: 'btn small', href: `#/tracker?date=${item.day}` }, 'Open day')));
    }
  }
  wrap.appendChild(exceptions);

  wrap.appendChild(privacyLink());
  return wrap;
}

function privacyLink() {
  const card = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'What this system records about you')),
    el('p', { class: 'hint' },
      'You can see exactly what is recorded, why, for how long and who can see it.'),
    el('div', { class: 'row' },
      el('button', {
        onClick: async () => {
          const { modal } = await import('../api.js');
          const notice = await api.get('/api/privacy/notice');
          const body = el('div', {});
          const section = (title, items) => {
            body.append(el('h3', {}, title),
              el('ul', {}, items.map((item) => el('li', {}, item))));
          };
          section('Recorded', notice.what_is_recorded);
          section('Never recorded', notice.what_is_not_recorded);
          section('Why', Object.values(notice.why));
          section('Who can see it', notice.who_can_see_it);
          body.append(el('h3', {}, 'How long'), el('p', {}, notice.how_long),
            el('h3', {}, 'Your rights'),
            el('ul', {}, Object.values(notice.your_rights).map((v) => el('li', {}, v))),
            el('p', { class: 'hint' }, notice.data_location));
          modal({ title: 'Privacy notice', body, actions: [{ label: 'Close', class: 'primary' }] });
        },
      }, 'Read the privacy notice'),
      el('button', {
        onClick: async () => {
          try {
            await api.downloadGet(`/api/users/${state.user.id}/data-export`,
              'my-timekeeper-data.json');
            toast('Your data export has been downloaded.', 'ok');
          } catch (error) { errorToast(error); }
        },
      }, 'Download everything held about me')));
  return card;
}

function stat(label, value, sub, tone) {
  return el('div', { class: 'stat' },
    el('div', { class: 'label' }, label),
    el('div', { class: `value ${tone || ''}` }, value),
    sub ? el('div', { class: 'sub' }, sub) : null);
}

/* ---------- Manager ---------- */

export async function renderManagerDashboard() {
  const data = await api.get('/api/dashboard/manager');
  const wrap = el('div');

  wrap.append(el('div', { class: 'grid cols-4' },
    stat('Clocked in', String(data.board.totals.in)),
    stat('On break', String(data.board.totals.on_break)),
    stat('Expected, not present', String(data.board.totals.expected), null,
      data.board.totals.expected ? 'neg' : ''),
    stat('Absent', String(data.board.totals.absent)),
  ));

  wrap.append(el('div', { class: 'grid cols-3' },
    actionCard('Timesheets awaiting approval', data.awaiting_approval, '#/approvals'),
    actionCard('Absence requests', data.pending_absence, '#/absence?tab=approvals'),
    actionCard('Correction requests', data.pending_corrections, '#/approvals?tab=corrections'),
  ));

  /* Live team board (FR-I-08) */
  const board = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Live team board'),
      el('div', { class: 'spacer' }),
      el('span', { class: 'hint' }, `as of ${new Date().toLocaleTimeString()}`),
      el('button', { class: 'small', onClick: () => location.reload() }, 'Refresh')));
  const people = el('div', { class: 'board' });
  for (const row of data.board.rows) {
    people.appendChild(el('div', { class: `person ${row.status}` },
      el('div', { class: 'name' }, row.employee),
      el('div', { class: 'meta' }, STATUS_LABEL[row.status] || row.status),
      el('div', { class: 'meta' },
        row.since ? `since ${row.since}` : (row.team || ''))));
  }
  board.appendChild(data.board.rows.length ? people
    : el('div', { class: 'empty' }, 'No one in your scope.'));
  wrap.appendChild(board);

  /* Exception queue */
  const queue = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Exception queue'),
      el('span', { class: 'badge warn' }, String(data.exceptions.totals.open)),
      el('span', { class: 'badge err' }, `${data.exceptions.totals.blocking} blocking`)));
  if (!data.exceptions.rows.length) {
    queue.appendChild(el('div', { class: 'empty' }, 'Nothing outstanding.'));
  } else {
    const table = el('table', {},
      el('thead', {}, el('tr', {},
        el('th', {}, 'Employee'), el('th', {}, 'Date'), el('th', {}, 'Type'),
        el('th', {}, 'Detail'), el('th', { class: 'num' }, 'Age'), el('th', {}, ''))),
      el('tbody', {}, data.exceptions.rows.slice(0, 60).map((row) => el('tr', {},
        el('td', {}, row.employee),
        el('td', {}, row.date),
        el('td', {}, el('span', { class: `badge ${row.blocking ? 'err' : 'warn'}` },
          row.type.replace(/_/g, ' ').toLowerCase())),
        el('td', { class: 'wrap' }, row.detail),
        el('td', { class: 'num' }, `${row.age_days} d`),
        el('td', {}, el('button', {
          class: 'small',
          onClick: async () => {
            const note = await promptDialog('Resolve exception', 'Resolution note');
            if (!note) return;
            try {
              await api.post(`/api/attendance/exceptions/${row.id}/resolve`, { note });
              toast('Resolved.', 'ok');
              location.reload();
            } catch (error) { errorToast(error); }
          },
        }, 'Resolve'))))));
    queue.appendChild(el('div', { class: 'table-wrap' }, table));
  }
  wrap.appendChild(queue);
  return wrap;
}

function actionCard(label, count, href) {
  return el('a', {
    class: 'stat', href, style: 'text-decoration:none;color:inherit;display:block',
  },
    el('div', { class: 'label' }, label),
    el('div', { class: `value ${count ? 'neg' : ''}` }, String(count)),
    el('div', { class: 'sub' }, count ? 'waiting for you' : 'all clear'));
}
