/* Read-only view of a shared report (FR-I-12). The link carries no session and
   exposes nothing beyond the scope of the report that was shared. */

import { api, el, fmtDuration } from './api.js';

const token = location.pathname.split('/').pop();
const root = document.getElementById('root');

async function start() {
  let report;
  try {
    report = await api.get(`/api/shared/${token}`, { allowAnonymous: true });
  } catch (error) {
    root.appendChild(el('div', { class: 'card' },
      el('h1', {}, 'This link is not available'),
      el('p', {}, error.message)));
    return;
  }

  const durationFormat = report.duration_format || 'hm';
  const card = el('div', { class: 'card' },
    el('header', {}, el('h1', {}, report.title),
      el('div', { class: 'spacer' }),
      el('span', { class: 'badge info' }, 'shared, read-only')),
    el('p', { class: 'hint' },
      Object.entries(report.meta || {})
        .filter(([, value]) => typeof value !== 'object')
        .map(([key, value]) => `${key.replace(/_/g, ' ')}: ${value}`).join(' · ')));

  if (!report.rows.length) {
    card.appendChild(el('div', { class: 'empty' }, 'No rows.'));
  } else {
    card.appendChild(el('div', { class: 'table-wrap' },
      el('table', {},
        el('thead', {}, el('tr', {}, report.columns.map((column) =>
          el('th', { class: column.type === 'duration' ? 'num' : '' }, column.label)))),
        el('tbody', {}, report.rows.map((row) => el('tr', {}, report.columns.map((column) =>
          el('td', { class: column.type === 'duration' ? 'num' : 'wrap' },
            column.type === 'duration'
              ? fmtDuration(row[column.key], durationFormat)
              : String(row[column.key] ?? '')))))),
        report.totals && Object.keys(report.totals).length
          ? el('tfoot', {}, el('tr', {}, report.columns.map((column, index) =>
            el('td', { class: column.type === 'duration' ? 'num' : '' },
              column.key in report.totals
                ? (column.type === 'duration'
                  ? fmtDuration(report.totals[column.key], durationFormat)
                  : String(report.totals[column.key]))
                : (index === 0 ? 'TOTAL' : '')))))
          : null)));
  }
  root.appendChild(card);
}

start();
