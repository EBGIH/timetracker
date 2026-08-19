/* Audit log browser (FR-L-03). Records are append-only; nothing here can
   modify them. */

import { addDays, api, clear, el, errorToast, today } from '../api.js';

export async function renderAudit() {
  const [users] = await Promise.all([api.get('/api/users').catch(() => [])]);

  const actor = el('select', { id: 'a-actor', 'aria-label': 'Actor' },
    el('option', { value: '' }, 'Anyone'),
    users.map((u) => el('option', { value: u.id }, u.name)));
  const entity = el('select', { id: 'a-entity', 'aria-label': 'Entity type' },
    el('option', { value: '' }, 'Any entity'),
    ['attendance_session', 'break_record', 'time_entry', 'absence_request',
      'approval', 'period', 'user', 'credential', 'kiosk', 'organisation',
      'absence_policy', 'overtime_rule', 'payroll_export', 'api_key',
      'rule_param_version', 'correction_request', 'attendance_exception']
      .map((value) => el('option', { value }, value.replace(/_/g, ' '))));
  const action = el('input', { id: 'a-action', placeholder: 'e.g. period.', style: 'width:12rem' });
  const start = el('input', { type: 'date', value: addDays(today(), -30), id: 'a-from' });
  const end = el('input', { type: 'date', value: today(), id: 'a-to' });
  const output = el('div');

  async function load() {
    clear(output);
    output.appendChild(el('div', { class: 'empty' }, 'Loading…'));
    const query = new URLSearchParams({ start: start.value, end: end.value, limit: '300' });
    if (actor.value) query.set('actor_id', actor.value);
    if (entity.value) query.set('entity_type', entity.value);
    if (action.value) query.set('action', action.value);
    try {
      const rows = await api.get(`/api/audit?${query}`);
      clear(output);
      output.appendChild(table(rows));
    } catch (error) {
      clear(output);
      output.appendChild(el('div', { class: 'msg err' }, error.message));
    }
  }

  const controls = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Audit log'),
      el('div', { class: 'spacer' }),
      el('button', {
        onClick: () => api.downloadGet(
          `/api/audit/export?start=${start.value}&end=${end.value}`, 'audit_log.csv'),
      }, 'Export CSV')),
    el('p', { class: 'hint' },
      'Every create, update, delete and approval on attendance and absence data '
      + 'is recorded with actor, UTC timestamp, action, entity, previous value '
      + 'and new value. Records are append-only and cannot be modified or deleted '
      + 'by any application role. Credential material is redacted.'),
    el('div', { class: 'row' },
      el('div', { class: 'field' }, el('label', { for: 'a-actor' }, 'Actor'), actor),
      el('div', { class: 'field' }, el('label', { for: 'a-entity' }, 'Entity'), entity),
      el('div', { class: 'field' }, el('label', { for: 'a-action' }, 'Action prefix'), action),
      el('div', { class: 'field' }, el('label', { for: 'a-from' }, 'From'), start),
      el('div', { class: 'field' }, el('label', { for: 'a-to' }, 'To'), end),
      el('button', { class: 'primary', onClick: load }, 'Search')));

  const wrap = el('div', {}, controls, output);
  await load();
  return wrap;
}

function table(rows) {
  if (!rows.length) return el('div', { class: 'empty' }, 'No audit records match.');
  const body = rows.map((row) => {
    const detail = el('td', { class: 'wrap' });
    const diff = diffSummary(row.before, row.after);
    detail.append(...[
      row.note ? el('div', {}, row.note) : null,
      diff.length
        ? el('ul', { style: 'margin:.2rem 0 0 1rem;padding:0' },
          diff.slice(0, 8).map(([key, before, after]) =>
            el('li', {}, `${key}: `, el('s', {}, String(before)), ' → ', String(after))))
        : el('span', { class: 'hint' }, '—'),
    ].filter(Boolean));
    return el('tr', {},
      el('td', {}, new Date(row.occurred_at + 'Z').toLocaleString()),
      el('td', {}, row.actor, el('div', { class: 'hint' }, row.actor_role || '')),
      el('td', {}, el('span', { class: 'badge info' }, row.action)),
      el('td', {}, row.entity_type,
        el('div', { class: 'hint', style: 'font-family:var(--mono);font-size:.72rem' },
          (row.entity_id || '').slice(0, 12))),
      detail,
      el('td', {}, row.ip || ''));
  });
  return el('div', { class: 'card' },
    el('header', {}, el('h2', {}, `${rows.length} record(s)`)),
    el('div', { class: 'table-wrap', style: 'max-height:70vh' },
      el('table', {},
        el('thead', {}, el('tr', {},
          el('th', {}, 'When (local)'), el('th', {}, 'Actor'), el('th', {}, 'Action'),
          el('th', {}, 'Entity'), el('th', {}, 'Change'), el('th', {}, 'IP'))),
        el('tbody', {}, body))));
}

function diffSummary(before, after) {
  if (!before || !after) {
    if (after && !before) {
      return Object.entries(after)
        .filter(([, value]) => value !== null && value !== '' && value !== false)
        .slice(0, 6)
        .map(([key, value]) => [key, '—', value]);
    }
    return [];
  }
  const changed = [];
  for (const key of Object.keys(after)) {
    if (['updated_at', 'created_at', 'version'].includes(key)) continue;
    if (JSON.stringify(before[key]) !== JSON.stringify(after[key])) {
      changed.push([key, before[key] ?? '—', after[key] ?? '—']);
    }
  }
  return changed;
}
