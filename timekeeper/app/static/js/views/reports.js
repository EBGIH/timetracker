/* Report runner, saved/scheduled/shared reports and the payroll export
   (Modules I and J). */

import {
  addDays, api, can, clear, confirmDialog, el, errorToast, fmtDuration,
  promptDialog, state, toast, today,
} from '../api.js';

let filters = {
  start: today().slice(0, 8) + '01',
  end: today(),
  user_ids: null,
  team_ids: null,
  only_exceptions: false,
  group_by: 'employee',
  status: null,
};

export async function renderReports(params) {
  const tabs = ['run', 'saved'];
  if (can('payroll_export')) tabs.push('payroll');
  let active = params.get('tab') || 'run';
  if (!tabs.includes(active)) active = 'run';
  if (params.get('user')) filters.user_ids = [params.get('user')];

  const labels = { run: 'Run a report', saved: 'Saved & scheduled', payroll: 'Payroll export' };
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
  if (tab === 'saved') return savedPanel();
  if (tab === 'payroll') return payrollPanel();
  return runPanel();
}

/* ---------- Run a report ---------- */

async function runPanel() {
  const [catalogue, teams, users] = await Promise.all([
    api.get('/api/reports/catalogue'),
    api.get('/api/org/teams').catch(() => []),
    api.get('/api/users').catch(() => []),
  ]);

  let current = null;
  const typeSelect = el('select', { id: 'r-type', 'aria-label': 'Report' },
    catalogue.map((entry) => el('option', { value: entry.type },
      `${entry.title} — ${entry.grain}`)));
  const start = el('input', { type: 'date', value: filters.start, id: 'r-start' });
  const end = el('input', { type: 'date', value: filters.end, id: 'r-end' });
  const teamSelect = el('select', { id: 'r-team', 'aria-label': 'Team' },
    el('option', { value: '' }, 'All teams'),
    teams.map((t) => el('option', { value: t.id }, t.name)));
  const userSelect = el('select', { id: 'r-user', 'aria-label': 'Employee' },
    el('option', { value: '' }, 'All employees'),
    users.map((u) => el('option', {
      value: u.id, selected: filters.user_ids?.includes(u.id),
    }, u.name)));
  const groupBy = el('select', { id: 'r-group', 'aria-label': 'Group by' },
    ['employee', 'team', 'location', 'date', 'week'].map((g) =>
      el('option', { value: g }, g)));
  const onlyExceptions = el('input', { type: 'checkbox', id: 'r-exc' });

  const output = el('div');

  function collect() {
    filters = {
      start: start.value,
      end: end.value,
      team_ids: teamSelect.value ? [teamSelect.value] : null,
      user_ids: userSelect.value ? [userSelect.value] : null,
      only_exceptions: onlyExceptions.checked,
      group_by: groupBy.value,
      status: null,
    };
    return filters;
  }

  async function run() {
    clear(output);
    output.appendChild(el('div', { class: 'empty' }, 'Running…'));
    try {
      current = await api.post(`/api/reports/run/${typeSelect.value}`, collect());
      clear(output);
      output.appendChild(renderTable(current));
    } catch (error) {
      clear(output);
      output.appendChild(el('div', { class: 'msg err' }, error.message));
    }
  }

  const controls = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Report')),
    el('div', { class: 'row' },
      el('div', { class: 'field' }, el('label', { for: 'r-type' }, 'Report'), typeSelect),
      el('div', { class: 'field' }, el('label', { for: 'r-start' }, 'From'), start),
      el('div', { class: 'field' }, el('label', { for: 'r-end' }, 'To'), end),
      el('div', { class: 'field' }, el('label', { for: 'r-team' }, 'Team'), teamSelect),
      el('div', { class: 'field' }, el('label', { for: 'r-user' }, 'Employee'), userSelect),
      el('div', { class: 'field' }, el('label', { for: 'r-group' }, 'Group by'), groupBy),
      el('div', { class: 'checkbox' }, onlyExceptions,
        el('label', { for: 'r-exc' }, 'Only rows with exceptions')),
      el('button', { class: 'primary', onClick: run }, 'Run')),
    el('div', { class: 'row', style: 'margin-top:.5rem' },
      ...['csv', 'xlsx', 'pdf'].map((fmt) => el('button', {
        onClick: async () => {
          try {
            await api.download(
              `/api/reports/run/${typeSelect.value}/export?fmt=${fmt}`, collect(),
              `${typeSelect.value}_${start.value}_${end.value}.${fmt}`);
          } catch (error) { errorToast(error); }
        },
      }, `Export ${fmt.toUpperCase()}`)),
      el('button', {
        onClick: async () => {
          const name = await promptDialog('Save this report',
            'Give the saved filter set a name.', { textarea: false });
          if (!name) return;
          try {
            await api.post('/api/reports/saved', {
              name, report_type: typeSelect.value, filters: collect(),
              schedule_cron: null, schedule_recipients: [],
            });
            toast('Saved.', 'ok');
          } catch (error) { errorToast(error); }
        },
      }, 'Save filter set'),
      el('button', {
        onClick: () => window.print(),
      }, 'Print')),
    el('p', { class: 'hint' },
      'Exports contain exactly the rows and totals shown here.'));

  const wrap = el('div', {}, controls, output);
  await run();
  return wrap;
}

function renderTable(report) {
  const durationFormat = report.duration_format || 'hm';
  const card = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, report.title),
      el('div', { class: 'spacer' }),
      el('span', { class: 'hint' }, `${report.rows.length} row(s)`)));

  if (report.meta) {
    card.appendChild(el('p', { class: 'hint' },
      Object.entries(report.meta)
        .filter(([, v]) => typeof v !== 'object')
        .map(([k, v]) => `${k.replace(/_/g, ' ')}: ${v}`).join(' · ')));
  }
  if (!report.rows.length) {
    card.appendChild(el('div', { class: 'empty' }, 'No rows matched the selected filters.'));
    return card;
  }

  const head = el('tr', {}, report.columns.map((column) =>
    el('th', { class: column.type === 'duration' ? 'num' : '' }, column.label)));
  const body = report.rows.map((row) => el('tr', {}, report.columns.map((column) => {
    const value = row[column.key];
    if (column.type === 'duration') {
      return el('td', { class: 'num' }, fmtDuration(value, durationFormat));
    }
    if (column.key === 'exceptions' && value) {
      return el('td', {}, String(value).split(', ').map((flag) =>
        el('span', { class: 'badge warn', style: 'margin-right:.25rem' },
          flag.replace(/_/g, ' ').toLowerCase())));
    }
    if (column.key === 'status' && value) {
      const tone = { open: 'warn', resolved: 'ok', cleared: 'mute' }[value] || 'info';
      return el('td', {}, el('span', { class: `badge ${tone}` }, value));
    }
    if (typeof value === 'boolean') {
      return el('td', {}, value ? 'yes' : 'no');
    }
    return el('td', { class: 'wrap' }, value === null || value === undefined ? '' : String(value));
  })));

  const hasColumnTotals = report.columns.some((column) => column.key in (report.totals || {}));
  const totals = hasColumnTotals
    ? el('tfoot', {}, el('tr', {}, report.columns.map((column, index) => {
      if (column.key in report.totals) {
        return el('td', { class: column.type === 'duration' ? 'num' : '' },
          column.type === 'duration'
            ? fmtDuration(report.totals[column.key], durationFormat)
            : String(report.totals[column.key]));
      }
      return el('td', {}, index === 0 ? 'TOTAL' : '');
    })))
    : null;

  card.appendChild(el('div', { class: 'table-wrap', style: 'max-height:70vh' },
    el('table', {}, el('thead', {}, head), el('tbody', {}, body), totals)));

  const extra = Object.entries(report.totals || {})
    .filter(([key]) => !report.columns.some((c) => c.key === key));
  if (extra.length) {
    card.appendChild(el('div', { class: 'grid cols-4', style: 'margin-top:.75rem' },
      extra.map(([key, value]) => el('div', { class: 'stat' },
        el('div', { class: 'label' }, key.replace(/_/g, ' ')),
        el('div', { class: 'value' }, String(value))))));
  }
  return card;
}

/* ---------- Saved / scheduled / shared ---------- */

async function savedPanel() {
  const rows = await api.get('/api/reports/saved/list');
  const card = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Saved reports')),
    el('p', { class: 'hint' },
      'A schedule uses five-field cron in UTC, for example "0 6 * * 1" for '
      + 'Mondays at 06:00. A share link runs with your visibility and expires.'));
  if (!rows.length) {
    card.appendChild(el('div', { class: 'empty' }, 'Nothing saved yet — run a report and press “Save filter set”.'));
    return card;
  }
  const table = el('table', {},
    el('thead', {}, el('tr', {},
      el('th', {}, 'Name'), el('th', {}, 'Report'), el('th', {}, 'Range'),
      el('th', {}, 'Schedule'), el('th', {}, 'Share link'), el('th', {}, ''))),
    el('tbody', {}, rows.map((row) => el('tr', {},
      el('td', {}, row.name),
      el('td', {}, row.report_type),
      el('td', {}, `${row.filters.start} → ${row.filters.end}`),
      el('td', {}, row.schedule_cron || '—'),
      el('td', {}, row.share_url
        ? el('a', { href: row.share_url, target: '_blank', rel: 'noopener' }, 'open')
        : '—'),
      el('td', {},
        el('button', {
          class: 'small',
          onClick: async () => {
            const cron = await promptDialog('Schedule delivery',
              'Cron expression (UTC), e.g. "0 6 1 * *" for the first of the month at 06:00.',
              { textarea: false });
            if (!cron) return;
            try {
              await api.post('/api/reports/saved', {
                name: row.name, report_type: row.report_type, filters: row.filters,
                schedule_cron: cron, schedule_recipients: row.schedule_recipients,
              });
              await api.del(`/api/reports/saved/${row.id}`);
              toast('Schedule set.', 'ok');
              location.reload();
            } catch (error) { errorToast(error); }
          },
        }, 'Schedule'),
        el('button', {
          class: 'small',
          onClick: async () => {
            try {
              const result = await api.post(`/api/reports/saved/${row.id}/share`,
                { expires_in_days: 7 });
              toast(`Shared until ${result.expires_at}.`, 'ok');
              location.reload();
            } catch (error) { errorToast(error); }
          },
        }, 'Share'),
        el('button', {
          class: 'small',
          onClick: async () => {
            if (!await confirmDialog('Delete', `Delete “${row.name}”?`)) return;
            await api.del(`/api/reports/saved/${row.id}`);
            location.reload();
          },
        }, 'Delete'))))));
  card.appendChild(el('div', { class: 'table-wrap' }, table));
  return card;
}

/* ---------- Payroll export (Module J) ---------- */

async function payrollPanel() {
  const [periods, layouts, columns] = await Promise.all([
    api.get('/api/periods?count=12'),
    api.get('/api/payroll/layouts'),
    api.get('/api/payroll/columns'),
  ]);

  const periodSelect = el('select', { id: 'p-period', 'aria-label': 'Period' },
    periods.map((p) => el('option', { value: p.id },
      `${p.start_date} – ${p.end_date}${p.status === 'locked' ? ' · locked' : ' · open'}`)));
  const layoutSelect = el('select', { id: 'p-layout', 'aria-label': 'Layout' },
    layouts.map((l) => el('option', { value: l.id }, l.name)));
  const result = el('div');

  const generate = el('button', {
    class: 'primary',
    onClick: async () => {
      await runExport(false);
    },
  }, 'Generate export');

  async function runExport(confirmUnlocked) {
    clear(result);
    result.appendChild(el('div', { class: 'empty' }, 'Generating…'));
    try {
      const data = await api.post('/api/payroll/exports', {
        period_id: periodSelect.value,
        layout_id: layoutSelect.value,
        confirm_unlocked: confirmUnlocked,
      });
      clear(result);
      result.appendChild(exportResult(data));
    } catch (error) {
      clear(result);
      if (error.payload?.error === 'period_not_locked') {
        result.appendChild(el('div', { class: 'msg warn' },
          error.payload.message, ' ',
          el('button', { class: 'small', onClick: () => runExport(true) },
            'Export anyway')));
      } else {
        result.appendChild(el('div', { class: 'msg err' }, error.message));
      }
    }
  }

  function exportResult(data) {
    const recon = data.reconciliation;
    const card = el('div', { class: 'card' },
      el('header', {}, el('h2', {}, 'Export generated'),
        el('span', { class: data.period_locked ? 'badge ok' : 'badge warn' },
          data.period_locked ? 'period locked' : 'period NOT locked'),
        el('div', { class: 'spacer' }),
        el('button', {
          class: 'primary',
          onClick: () => api.downloadGet(`/api/payroll/exports/${data.id}/download`,
            `payroll_${data.id}.csv`),
        }, 'Download')),
      el('div', { class: 'grid cols-3' },
        el('div', { class: 'stat' }, el('div', { class: 'label' }, 'Rows'),
          el('div', { class: 'value' }, String(data.row_count))),
        el('div', { class: 'stat' }, el('div', { class: 'label' }, 'Checksum (SHA-256)'),
          el('div', { class: 'sub', style: 'font-family:var(--mono);word-break:break-all' },
            data.checksum)),
        el('div', { class: 'stat' }, el('div', { class: 'label' }, 'Changed since last export'),
          el('div', { class: `value ${recon.changes.length ? 'neg' : 'pos'}` },
            String(recon.changes.length)))),
      el('h3', { style: 'margin-top:1rem' }, 'Preview'),
      el('pre', {
        style: 'background:#0e1a2b;color:#dce6f2;padding:.75rem;border-radius:8px;overflow-x:auto;font-size:.8rem',
      }, data.preview.join('\n')));

    if (recon.changes.length) {
      const table = el('table', {},
        el('thead', {}, el('tr', {},
          el('th', {}, 'Personnel no.'), el('th', {}, 'Employee'),
          el('th', {}, 'Field'), el('th', { class: 'num' }, 'Previous'),
          el('th', { class: 'num' }, 'Current'), el('th', { class: 'num' }, 'Delta'))),
        el('tbody', {}, recon.changes.flatMap((change) =>
          Object.entries(change.fields).map(([field, delta]) => el('tr', {},
            el('td', {}, change.personnel_number),
            el('td', {}, change.employee),
            el('td', {}, field.replace(/_/g, ' ')),
            el('td', { class: 'num' }, fmtDuration(delta.previous)),
            el('td', { class: 'num' }, fmtDuration(delta.current)),
            el('td', { class: 'num' }, fmtDuration(delta.delta)))))));
      card.append(el('h3', { style: 'margin-top:1rem' }, 'Reconciliation'),
        el('div', { class: 'table-wrap' }, table));
    } else {
      card.appendChild(el('p', { class: 'hint' }, recon.note
        || 'No employee figures changed since the previous export for this period.'));
    }
    return card;
  }

  const history = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Export history'),
      el('div', { class: 'spacer' }),
      el('button', {
        class: 'small',
        onClick: async () => {
          const rows = await api.get('/api/payroll/exports');
          const table = el('table', {},
            el('thead', {}, el('tr', {},
              el('th', {}, 'Generated'), el('th', { class: 'num' }, 'Rows'),
              el('th', {}, 'Locked'), el('th', {}, 'Checksum'), el('th', {}, ''))),
            el('tbody', {}, rows.map((row) => el('tr', {},
              el('td', {}, new Date(row.generated_at + 'Z').toLocaleString()),
              el('td', { class: 'num' }, String(row.row_count)),
              el('td', {}, row.period_locked ? 'yes' : 'no'),
              el('td', { style: 'font-family:var(--mono);font-size:.75rem' },
                row.checksum.slice(0, 16) + '…'),
              el('td', {}, el('button', {
                class: 'small',
                onClick: () => api.downloadGet(`/api/payroll/exports/${row.id}/download`,
                  `payroll_${row.id}.csv`),
              }, 'Download'))))));
          const holder = history.querySelector('.history-table');
          if (holder) holder.remove();
          history.appendChild(el('div', { class: 'table-wrap history-table' }, table));
        },
      }, 'Load')),
    el('p', { class: 'hint' },
      'Every export is recorded with who, when, scope and checksum, and can be '
      + 're-downloaded byte for byte.'));

  const layoutCard = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Layout')),
    el('p', { class: 'hint' },
      `Available columns: ${[...columns.base, ...columns.absence_by_policy].join(', ')}`),
    can('configure_policies') ? el('button', {
      class: 'small',
      onClick: () => editLayout(layouts.find((l) => l.id === layoutSelect.value), columns),
    }, 'Edit layout') : null);

  return el('div', {},
    el('div', { class: 'card' },
      el('header', {}, el('h2', {}, 'Payroll export')),
      el('div', { class: 'row' },
        el('div', { class: 'field' }, el('label', { for: 'p-period' }, 'Period'), periodSelect),
        el('div', { class: 'field' }, el('label', { for: 'p-layout' }, 'Layout'), layoutSelect),
        generate)),
    result, layoutCard, history);
}

async function editLayout(layout, columns) {
  const { modal } = await import('../api.js');
  const all = [...columns.base, ...columns.absence_by_policy];
  const boxes = all.map((column) => {
    const input = el('input', {
      type: 'checkbox', checked: layout.columns.includes(column),
      dataset: { column },
    });
    return el('div', { class: 'checkbox' }, input, el('label', {}, column));
  });
  const delimiter = el('input', { value: layout.delimiter, maxlength: '3', style: 'width:4rem' });
  const durationFormat = el('select', {},
    ['decimal', 'hm', 'minutes'].map((f) =>
      el('option', { value: f, selected: layout.duration_format === f }, f)));
  const encoding = el('input', { value: layout.encoding });

  modal({
    title: `Payroll layout — ${layout.name}`,
    body: el('div', {},
      el('div', { class: 'row' },
        el('div', { class: 'field' }, el('label', {}, 'Delimiter'), delimiter),
        el('div', { class: 'field' }, el('label', {}, 'Duration format'), durationFormat),
        el('div', { class: 'field' }, el('label', {}, 'Encoding'), encoding)),
      el('fieldset', {}, el('legend', {}, 'Columns, in order'), boxes)),
    actions: [
      { label: 'Cancel' },
      {
        label: 'Save', class: 'primary', onClick: async (dialog) => {
          const selected = boxes
            .map((node) => node.querySelector('input'))
            .filter((input) => input.checked)
            .map((input) => input.dataset.column);
          try {
            await api.put(`/api/payroll/layouts/${layout.id}`, {
              name: layout.name, columns: selected, delimiter: delimiter.value,
              encoding: encoding.value, date_format: layout.date_format,
              duration_format: durationFormat.value, include_header: layout.include_header,
            });
            dialog.close();
            toast('Layout saved.', 'ok');
          } catch (error) { errorToast(error); }
        },
      },
    ],
  });
}
