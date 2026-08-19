/* Administration: organisation settings, structure, people, policies, kiosks,
   working-time rule parameters and integrations. NFR-M-01: all of this is
   changeable here, without a code release. */

import {
  api, can, clear, confirmDialog, el, errorToast, fmtDuration, modal,
  promptDialog, state, toast, today,
} from '../api.js';

const TABS = [
  ['people', 'People'],
  ['structure', 'Teams & sites'],
  ['policies', 'Policies'],
  ['rules', 'Working-time rules'],
  ['kiosks', 'Kiosks'],
  ['holidays', 'Holidays'],
  ['settings', 'Workspace'],
  ['integrations', 'Integrations'],
];

export async function renderAdmin(params) {
  let active = params.get('tab') || 'people';
  const bar = el('div', { class: 'tabs', role: 'tablist' });
  const body = el('div');
  const allowed = TABS.filter(([key]) =>
    key === 'people' ? can('manage_users')
      : key === 'integrations' || key === 'settings' ? can('configure_org')
        : can('configure_policies'));
  if (!allowed.some(([key]) => key === active)) active = allowed[0][0];

  for (const [key, label] of allowed) {
    bar.appendChild(el('button', {
      role: 'tab', 'aria-selected': String(key === active),
      onClick: async () => {
        bar.querySelectorAll('button').forEach((b, i) =>
          b.setAttribute('aria-selected', String(allowed[i][0] === key)));
        clear(body);
        body.appendChild(el('div', { class: 'empty' }, 'Loading…'));
        const node = await panel(key);
        clear(body);
        body.appendChild(node);
      },
    }, label));
  }
  body.appendChild(await panel(active));
  return el('div', {}, bar, body);
}

async function panel(key) {
  switch (key) {
    case 'structure': return structurePanel();
    case 'policies': return policiesPanel();
    case 'rules': return rulesPanel();
    case 'kiosks': return kiosksPanel();
    case 'holidays': return holidaysPanel();
    case 'settings': return settingsPanel();
    case 'integrations': return integrationsPanel();
    default: return peoplePanel();
  }
}

function field(label, node) {
  return el('div', { class: 'field' }, el('label', {}, label), node);
}

/* ---------- People (Module B) ---------- */

async function peoplePanel() {
  const [users, teams, locations] = await Promise.all([
    api.get('/api/users?include_inactive=true'),
    api.get('/api/org/teams'),
    api.get('/api/org/locations'),
  ]);
  const teamName = Object.fromEntries(teams.map((t) => [t.id, t.name]));
  const locationName = Object.fromEntries(locations.map((l) => [l.id, l.name]));

  const table = el('table', {},
    el('thead', {}, el('tr', {},
      el('th', {}, 'Personnel no.'), el('th', {}, 'Name'), el('th', {}, 'Role'),
      el('th', {}, 'Team'), el('th', {}, 'Site'), el('th', {}, 'Contract'),
      el('th', {}, 'Status'), el('th', {}, ''))),
    el('tbody', {}, users.map((user) => el('tr', {},
      el('td', {}, user.personnel_number),
      el('td', {}, user.name,
        user.email ? el('div', { class: 'hint' }, user.email)
          : el('div', { class: 'hint' }, 'limited member — kiosk only')),
      el('td', {}, el('span', { class: 'badge info' }, user.role)),
      el('td', {}, teamName[user.team_id] || '—'),
      el('td', {}, locationName[user.location_id] || '—'),
      el('td', {}, user.working_pattern
        ? `${user.working_pattern.contracted_hours_per_week} h/week`
        : el('span', { class: 'badge err' }, 'no pattern')),
      el('td', {}, el('span', { class: user.status === 'active' ? 'badge ok' : 'badge mute' },
        user.status)),
      el('td', {},
        el('button', { class: 'small', onClick: () => editUser(user, teams, locations) }, 'Edit'),
        el('button', { class: 'small', onClick: () => patternDialog(user) }, 'Pattern'),
        user.has_login
          ? el('button', {
            class: 'small',
            onClick: async () => {
              try {
                const result = await api.post(`/api/users/${user.id}/invite`);
                modal({
                  title: 'Invitation created',
                  body: el('div', {},
                    el('p', {}, 'Send this link to the employee. It expires on '
                      + new Date(result.expires_at + 'Z').toLocaleString() + '.'),
                    el('code', { style: 'word-break:break-all' },
                      location.origin + result.invite_url)),
                  actions: [{ label: 'Close', class: 'primary' }],
                });
              } catch (error) { errorToast(error); }
            },
          }, 'Invite')
          : el('button', {
            class: 'small',
            onClick: async () => {
              try {
                const result = await api.post(`/api/users/${user.id}/pin?digits=4`);
                modal({
                  title: `PIN for ${user.name}`,
                  body: el('div', {},
                    el('div', {
                      style: 'font-size:2.5rem;font-family:var(--mono);letter-spacing:.4rem;text-align:center;padding:1rem',
                    }, result.pin),
                    el('p', { class: 'msg warn' }, result.notice)),
                  actions: [{ label: 'Done', class: 'primary' }],
                });
              } catch (error) { errorToast(error); }
            },
          }, 'Issue PIN'),
        user.status === 'active' ? el('button', {
          class: 'small',
          onClick: async () => {
            if (!await confirmDialog('Deactivate',
              `${user.name} will no longer be able to clock in. Historic records are kept and remain reportable.`)) return;
            try {
              await api.post(`/api/users/${user.id}/deactivate`);
              toast('Deactivated.', 'ok');
              location.reload();
            } catch (error) { errorToast(error); }
          },
        }, 'Deactivate') : null)))));

  return el('div', {},
    el('div', { class: 'card' },
      el('header', {}, el('h2', {}, 'People'),
        el('span', { class: 'badge info' }, `${users.filter((u) => u.status === 'active').length} active`),
        el('div', { class: 'spacer' }),
        el('button', { onClick: () => importDialog() }, 'Bulk import (CSV)'),
        el('button', { class: 'primary', onClick: () => editUser(null, teams, locations) },
          'Add employee')),
      el('div', { class: 'table-wrap' }, table)));
}

function editUser(user, teams, locations) {
  const isNew = !user;
  const fields = {
    personnel_number: el('input', { value: user?.personnel_number || '', disabled: !isNew }),
    first_name: el('input', { value: user?.first_name || '' }),
    last_name: el('input', { value: user?.last_name || '' }),
    email: el('input', { type: 'email', value: user?.email || '' }),
    role: el('select', {}, ['employee', 'limited', 'manager', 'hr', 'admin'].map((role) =>
      el('option', { value: role, selected: user?.role === role }, role))),
    team_id: el('select', {}, el('option', { value: '' }, '—'),
      teams.map((t) => el('option', { value: t.id, selected: user?.team_id === t.id }, t.name))),
    location_id: el('select', {}, el('option', { value: '' }, '—'),
      locations.map((l) => el('option', { value: l.id, selected: user?.location_id === l.id }, l.name))),
    employment_start: el('input', { type: 'date', value: user?.employment_start || today() }),
  };

  modal({
    title: isNew ? 'Add employee' : `Edit ${user.name}`,
    body: el('div', {},
      el('div', { class: 'row' },
        field('Personnel number', fields.personnel_number),
        field('First name', fields.first_name),
        field('Last name', fields.last_name)),
      el('div', { class: 'row' },
        field('E-mail (leave blank for a kiosk-only limited member)', fields.email),
        field('Role', fields.role)),
      el('div', { class: 'row' },
        field('Team', fields.team_id),
        field('Site', fields.location_id),
        field('Employment start', fields.employment_start)),
      el('p', { class: 'hint' },
        'A limited member has no login and no e-mail address; they clock in at a '
        + 'kiosk with a PIN or QR code.')),
    actions: [
      { label: 'Cancel' },
      {
        label: 'Save', class: 'primary', onClick: async (dialog) => {
          const role = fields.role.value;
          const email = fields.email.value.trim();
          const body = {
            first_name: fields.first_name.value.trim(),
            last_name: fields.last_name.value.trim(),
            email: email || null,
            role,
            team_id: fields.team_id.value || null,
            location_id: fields.location_id.value || null,
          };
          try {
            if (isNew) {
              await api.post('/api/users', {
                ...body,
                personnel_number: fields.personnel_number.value.trim(),
                employment_start: fields.employment_start.value,
                has_login: role !== 'limited',
                language: 'en',
              });
            } else {
              await api.put(`/api/users/${user.id}`, body);
            }
            dialog.close();
            toast('Saved.', 'ok');
            location.reload();
          } catch (error) { errorToast(error); }
        },
      },
    ],
  });
}

async function patternDialog(user) {
  const rows = await api.get(`/api/users/${user.id}/patterns`);
  const validFrom = el('input', { type: 'date', value: today() });
  const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const current = rows[rows.length - 1]?.expected_minutes || [480, 480, 480, 480, 480, 0, 0];
  const inputs = days.map((label, index) => el('input', {
    type: 'number', min: '0', max: '1440', step: '15', value: String(current[index] || 0),
    style: 'width:5rem', 'aria-label': label,
  }));
  const shiftStart = el('input', { type: 'time', value: rows[rows.length - 1]?.shift_start || '' });
  const shiftEnd = el('input', { type: 'time', value: rows[rows.length - 1]?.shift_end || '' });

  modal({
    title: `Working pattern — ${user.name}`,
    body: el('div', {},
      el('div', { class: 'table-wrap' },
        el('table', {},
          el('thead', {}, el('tr', {},
            el('th', {}, 'Valid from'), el('th', {}, 'To'),
            el('th', { class: 'num' }, 'Hours/week'), el('th', {}, 'Shift'))),
          el('tbody', {}, rows.map((row) => el('tr', {},
            el('td', {}, row.valid_from),
            el('td', {}, row.valid_to || 'current'),
            el('td', { class: 'num' }, String(row.contracted_hours_per_week)),
            el('td', {}, row.shift_start ? `${row.shift_start}–${row.shift_end}` : '—')))))),
      el('h3', { style: 'margin-top:1rem' }, 'New pattern'),
      el('div', { class: 'row' }, field('Effective from', validFrom)),
      el('div', { class: 'row' },
        days.map((label, index) => field(label, inputs[index]))),
      el('div', { class: 'row' },
        field('Shift start (optional)', shiftStart),
        field('Shift end (optional)', shiftEnd)),
      el('p', { class: 'hint' },
        'Minutes expected per weekday. A retrospective effective date recalculates '
        + 'the affected days and flags any that fall in a locked period.')),
    actions: [
      { label: 'Cancel' },
      {
        label: 'Add pattern', class: 'primary', onClick: async (dialog) => {
          const expected = inputs.map((input) => Number(input.value || 0));
          try {
            const result = await api.post(`/api/users/${user.id}/patterns`, {
              valid_from: validFrom.value,
              contracted_hours_per_week: expected.reduce((a, b) => a + b, 0) / 60,
              expected_minutes: expected,
              shift_start: shiftStart.value || null,
              shift_end: shiftEnd.value || null,
            });
            dialog.close();
            if (result.locked_periods_affected?.length) {
              toast(`Saved. Locked periods affected: ${result.locked_periods_affected.join(', ')}`,
                'err', 9000);
            } else toast('Pattern saved and affected days recalculated.', 'ok');
            location.reload();
          } catch (error) { errorToast(error); }
        },
      },
    ],
  });
}

function importDialog() {
  const textarea = el('textarea', {
    style: 'width:100%;min-height:180px;font-family:var(--mono);font-size:.8rem',
    placeholder: 'personnel_number,first_name,last_name,email,role,team,location,employment_start,hours_per_week',
  });
  const output = el('div');
  modal({
    title: 'Bulk import employees',
    body: el('div', {},
      el('p', { class: 'hint' },
        'Paste CSV with a header row. A dry run validates every line and creates nothing.'),
      textarea, output),
    actions: [
      { label: 'Close' },
      {
        label: 'Dry run', onClick: async () => {
          await runImport(true);
        },
      },
      { label: 'Import', class: 'primary', onClick: async () => { await runImport(false); } },
    ],
  });

  async function runImport(dryRun) {
    clear(output);
    try {
      const result = await api.post('/api/users/import',
        { csv: textarea.value, dry_run: dryRun });
      output.appendChild(el('div', { class: result.valid === result.rows ? 'msg ok' : 'msg warn' },
        `${result.rows} row(s), ${result.valid} valid`
        + (dryRun ? ' — nothing was created.' : `, ${result.created} created.`)));
      const bad = result.results.filter((row) => row.errors.length);
      if (bad.length) {
        output.appendChild(el('ul', {}, bad.map((row) =>
          el('li', {}, `Line ${row.line} (${row.personnel_number}): ${row.errors.join('; ')}`))));
      }
      if (!dryRun && result.created) setTimeout(() => location.reload(), 1500);
    } catch (error) { errorToast(error); }
  }
}

/* ---------- Teams & sites ---------- */

async function structurePanel() {
  const [teams, locations, users] = await Promise.all([
    api.get('/api/org/teams'), api.get('/api/org/locations'), api.get('/api/users'),
  ]);
  const userName = Object.fromEntries(users.map((u) => [u.id, u.name]));

  const teamCard = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Teams'),
      el('div', { class: 'spacer' }),
      el('button', { class: 'primary', onClick: () => teamDialog(null, teams, users) }, 'Add team')),
    el('div', { class: 'table-wrap' },
      el('table', {},
        el('thead', {}, el('tr', {}, el('th', {}, 'Team'), el('th', {}, 'Parent'),
          el('th', {}, 'Manager'), el('th', { class: 'num' }, 'Members'), el('th', {}, ''))),
        el('tbody', {}, teams.map((team) => el('tr', {},
          el('td', {}, team.name),
          el('td', {}, teams.find((t) => t.id === team.parent_team_id)?.name || '—'),
          el('td', {}, userName[team.manager_user_id] || '—'),
          el('td', { class: 'num' }, String(team.member_count)),
          el('td', {}, el('button', {
            class: 'small', onClick: () => teamDialog(team, teams, users),
          }, 'Edit'))))))));

  const locationCard = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Sites'),
      el('div', { class: 'spacer' }),
      el('button', { class: 'primary', onClick: () => locationDialog(null) }, 'Add site')),
    el('div', { class: 'table-wrap' },
      el('table', {},
        el('thead', {}, el('tr', {}, el('th', {}, 'Site'), el('th', {}, 'Address'),
          el('th', {}, 'Time zone'), el('th', {}, 'Geofence'), el('th', {}, ''))),
        el('tbody', {}, locations.map((location) => el('tr', {},
          el('td', {}, location.name),
          el('td', {}, location.address || '—'),
          el('td', {}, location.timezone),
          el('td', {}, location.geo_radius_m ? `${location.geo_radius_m} m` : '—'),
          el('td', {}, el('button', {
            class: 'small', onClick: () => locationDialog(location),
          }, 'Edit'))))))),
    el('p', { class: 'hint' },
      'A geofence records only whether the clock-in was inside the site radius — '
      + 'never a location track, and never raw coordinates.'));

  return el('div', {}, teamCard, locationCard);
}

function teamDialog(team, teams, users) {
  const name = el('input', { value: team?.name || '' });
  const parent = el('select', {}, el('option', { value: '' }, '— top level —'),
    teams.filter((t) => t.id !== team?.id).map((t) =>
      el('option', { value: t.id, selected: team?.parent_team_id === t.id }, t.name)));
  const manager = el('select', {}, el('option', { value: '' }, '— none —'),
    users.map((u) => el('option', { value: u.id, selected: team?.manager_user_id === u.id }, u.name)));
  modal({
    title: team ? `Edit ${team.name}` : 'Add team',
    body: el('div', {}, field('Name', name), field('Parent team', parent),
      field('Manager', manager)),
    actions: [{ label: 'Cancel' }, {
      label: 'Save', class: 'primary', onClick: async (dialog) => {
        const body = {
          name: name.value.trim(),
          parent_team_id: parent.value || null,
          manager_user_id: manager.value || null,
        };
        try {
          if (team) await api.put(`/api/org/teams/${team.id}`, body);
          else await api.post('/api/org/teams', body);
          dialog.close(); toast('Saved.', 'ok'); location.reload();
        } catch (error) { errorToast(error); }
      },
    }],
  });
}

function locationDialog(location) {
  const name = el('input', { value: location?.name || '' });
  const address = el('input', { value: location?.address || '' });
  const timezone = el('input', { value: location?.timezone || state.user.organisation.timezone });
  const lat = el('input', { type: 'number', step: 'any', value: location?.geo_lat ?? '' });
  const lng = el('input', { type: 'number', step: 'any', value: location?.geo_lng ?? '' });
  const radius = el('input', { type: 'number', value: location?.geo_radius_m ?? '' });
  modal({
    title: location ? `Edit ${location.name}` : 'Add site',
    body: el('div', {},
      field('Name', name), field('Address', address), field('Time zone', timezone),
      el('div', { class: 'row' }, field('Latitude', lat), field('Longitude', lng),
        field('Geofence radius (m)', radius))),
    actions: [{ label: 'Cancel' }, {
      label: 'Save', class: 'primary', onClick: async (dialog) => {
        const body = {
          name: name.value.trim(), address: address.value, timezone: timezone.value,
          geo_lat: lat.value ? Number(lat.value) : null,
          geo_lng: lng.value ? Number(lng.value) : null,
          geo_radius_m: radius.value ? Number(radius.value) : null,
        };
        try {
          if (location) await api.put(`/api/org/locations/${location.id}`, body);
          else await api.post('/api/org/locations', body);
          dialog.close(); toast('Saved.', 'ok'); location.reload();
        } catch (error) { errorToast(error); }
      },
    }],
  });
}

/* ---------- Policies ---------- */

async function policiesPanel() {
  const [policies, breakTypes, overtime, centres] = await Promise.all([
    api.get('/api/org/absence-policies'),
    api.get('/api/org/break-types'),
    api.get('/api/org/overtime-rule'),
    api.get('/api/org/cost-centres'),
  ]);

  const absenceCard = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Absence policies'),
      el('div', { class: 'spacer' }),
      el('button', { class: 'primary', onClick: () => policyDialog(null) }, 'Add policy')),
    el('div', { class: 'table-wrap' },
      el('table', {},
        el('thead', {}, el('tr', {}, el('th', {}, 'Policy'), el('th', {}, 'Paid'),
          el('th', {}, 'Accrual'), el('th', { class: 'num' }, 'Days/yr'),
          el('th', { class: 'num' }, 'Carry-over'), el('th', { class: 'num' }, 'Notice'),
          el('th', {}, 'Approvers'), el('th', {}, 'Document'), el('th', {}, ''))),
        el('tbody', {}, policies.map((policy) => el('tr', {},
          el('td', {}, policy.name),
          el('td', {}, policy.is_paid ? 'yes' : 'no'),
          el('td', {}, policy.accrual_method),
          el('td', { class: 'num' }, String(policy.accrual_rate_days)),
          el('td', { class: 'num' }, String(policy.carry_over_limit_days)),
          el('td', { class: 'num' }, `${policy.notice_days} d`),
          el('td', {}, (policy.approver_chain || []).join(' → ')),
          el('td', {}, policy.requires_document ? 'required' : '—'),
          el('td', {}, el('button', {
            class: 'small', onClick: () => policyDialog(policy),
          }, 'Edit'))))))),
    el('div', { class: 'row', style: 'margin-top:.6rem' },
      el('button', {
        onClick: async () => {
          const year = await promptDialog('Run carry-over',
            'Which year should be rolled into the next?', { textarea: false });
          if (!year) return;
          try {
            const result = await api.post('/api/absence/carry-over', { from_year: Number(year) });
            toast(`Carry-over run: ${result.records_updated} balance record(s) updated.`, 'ok');
          } catch (error) { errorToast(error); }
        },
      }, 'Run year-end carry-over')));

  const breakCard = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Break types'),
      el('div', { class: 'spacer' }),
      el('button', { class: 'primary', onClick: () => breakDialog(null) }, 'Add break type')),
    el('div', { class: 'table-wrap' },
      el('table', {},
        el('thead', {}, el('tr', {}, el('th', {}, 'Name'), el('th', {}, 'Paid'),
          el('th', { class: 'num' }, 'Max'), el('th', {}, ''))),
        el('tbody', {}, breakTypes.map((type) => el('tr', {},
          el('td', {}, type.name),
          el('td', {}, type.is_paid ? 'paid — counts as worked time' : 'unpaid — deducted'),
          el('td', { class: 'num' }, type.max_minutes ? `${type.max_minutes} min` : '—'),
          el('td', {}, el('button', {
            class: 'small', onClick: () => breakDialog(type),
          }, 'Edit'))))))));

  const overtimeCard = overtimeForm(overtime);

  const centreCard = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Cost centres'),
      el('div', { class: 'spacer' }),
      el('button', {
        class: 'primary',
        onClick: async () => {
          const code = await promptDialog('New cost centre', 'Code', { textarea: false });
          if (!code) return;
          const name = await promptDialog('New cost centre', 'Name', { textarea: false });
          if (!name) return;
          await api.post('/api/org/cost-centres', { code, name });
          toast('Added.', 'ok'); location.reload();
        },
      }, 'Add')),
    el('div', { class: 'row' }, centres.map((centre) =>
      el('span', { class: 'badge info' }, `${centre.code} — ${centre.name}`))));

  return el('div', {}, absenceCard, breakCard, overtimeCard, centreCard);
}

function overtimeForm(rule) {
  const daily = el('input', { type: 'number', value: rule.daily_threshold_minutes ?? '' });
  const weekly = el('input', { type: 'number', value: rule.weekly_threshold_minutes ?? '' });
  const priorApproval = el('input', { type: 'checkbox', checked: rule.requires_prior_approval });
  const nightStart = el('input', { type: 'time', value: rule.night_start });
  const nightEnd = el('input', { type: 'time', value: rule.night_end });
  const timeBank = el('input', { type: 'checkbox', checked: rule.time_bank_enabled });
  const cap = el('input', { type: 'number', value: rule.time_bank_cap_minutes });

  return el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Overtime and capacity')),
    el('div', { class: 'row' },
      field('Daily threshold (minutes)', daily),
      field('Weekly threshold (minutes)', weekly),
      field('Night starts', nightStart),
      field('Night ends', nightEnd)),
    el('div', { class: 'row' },
      el('div', { class: 'checkbox' }, priorApproval,
        el('label', {}, 'Overtime requires prior approval')),
      el('div', { class: 'checkbox' }, timeBank, el('label', {}, 'Enable the time bank')),
      field('Time-bank cap (minutes)', cap)),
    el('p', { class: 'hint' },
      'Overtime is calculated on net worked time only, never on gross presence. '
      + 'Categories are mutually exclusive with the priority public holiday → '
      + 'weekend → night → standard, so payroll never multiplies a minute twice.'),
    el('button', {
      class: 'primary',
      onClick: async () => {
        try {
          await api.put('/api/org/overtime-rule', {
            daily_threshold_minutes: daily.value ? Number(daily.value) : null,
            weekly_threshold_minutes: weekly.value ? Number(weekly.value) : null,
            requires_prior_approval: priorApproval.checked,
            night_start: nightStart.value, night_end: nightEnd.value,
            weekend_days: rule.weekend_days,
            time_bank_enabled: timeBank.checked,
            time_bank_cap_minutes: Number(cap.value || 0),
            time_bank_carry_over: rule.time_bank_carry_over,
          });
          toast('Saved.', 'ok');
        } catch (error) { errorToast(error); }
      },
    }, 'Save overtime rule'));
}

function policyDialog(policy) {
  const fields = {
    name: el('input', { value: policy?.name || '' }),
    code: el('input', { value: policy?.code || '' }),
    is_paid: el('input', { type: 'checkbox', checked: policy ? policy.is_paid : true }),
    accrual_method: el('select', {}, ['annual', 'monthly', 'unlimited'].map((m) =>
      el('option', { value: m, selected: policy?.accrual_method === m }, m))),
    accrual_rate_days: el('input', { type: 'number', step: '0.5', value: policy?.accrual_rate_days ?? 25 }),
    carry_over_limit_days: el('input', { type: 'number', step: '0.5', value: policy?.carry_over_limit_days ?? 0 }),
    allow_negative: el('input', { type: 'checkbox', checked: policy?.allow_negative || false }),
    notice_days: el('input', { type: 'number', value: policy?.notice_days ?? 0 }),
    requires_document: el('input', { type: 'checkbox', checked: policy?.requires_document || false }),
    approver_chain: el('input', { value: (policy?.approver_chain || ['manager']).join(', ') }),
    min_team_coverage: el('input', { type: 'number', value: policy?.min_team_coverage ?? 0 }),
    funded_from_time_bank: el('input', { type: 'checkbox', checked: policy?.funded_from_time_bank || false }),
  };
  modal({
    title: policy ? `Edit ${policy.name}` : 'Add absence policy',
    body: el('div', {},
      el('div', { class: 'row' }, field('Name', fields.name), field('Code', fields.code),
        el('div', { class: 'checkbox' }, fields.is_paid, el('label', {}, 'Paid'))),
      el('div', { class: 'row' },
        field('Accrual method', fields.accrual_method),
        field('Days per year', fields.accrual_rate_days),
        field('Carry-over limit (days)', fields.carry_over_limit_days)),
      el('div', { class: 'row' },
        field('Minimum notice (days)', fields.notice_days),
        field('Minimum team coverage', fields.min_team_coverage),
        field('Approver chain (comma separated: manager, hr)', fields.approver_chain)),
      el('div', { class: 'row' },
        el('div', { class: 'checkbox' }, fields.allow_negative,
          el('label', {}, 'Allow a negative balance')),
        el('div', { class: 'checkbox' }, fields.requires_document,
          el('label', {}, 'Requires a document')),
        el('div', { class: 'checkbox' }, fields.funded_from_time_bank,
          el('label', {}, 'Funded from the time bank')))),
    actions: [{ label: 'Cancel' }, {
      label: 'Save', class: 'primary', onClick: async (dialog) => {
        const body = {
          name: fields.name.value.trim(), code: fields.code.value.trim(),
          is_paid: fields.is_paid.checked,
          accrual_method: fields.accrual_method.value,
          accrual_rate_days: Number(fields.accrual_rate_days.value || 0),
          carry_over_limit_days: Number(fields.carry_over_limit_days.value || 0),
          carry_over_expiry_month: null,
          allow_negative: fields.allow_negative.checked,
          notice_days: Number(fields.notice_days.value || 0),
          requires_document: fields.requires_document.checked,
          approver_chain: fields.approver_chain.value.split(',').map((s) => s.trim()).filter(Boolean),
          min_team_coverage: Number(fields.min_team_coverage.value || 0),
          funded_from_time_bank: fields.funded_from_time_bank.checked,
        };
        try {
          if (policy) await api.put(`/api/org/absence-policies/${policy.id}`, body);
          else await api.post('/api/org/absence-policies', body);
          dialog.close(); toast('Saved.', 'ok'); location.reload();
        } catch (error) { errorToast(error); }
      },
    }],
  });
}

function breakDialog(type) {
  const name = el('input', { value: type?.name || '' });
  const isPaid = el('input', { type: 'checkbox', checked: type?.is_paid || false });
  const max = el('input', { type: 'number', value: type?.max_minutes ?? '' });
  modal({
    title: type ? `Edit ${type.name}` : 'Add break type',
    body: el('div', {}, field('Name', name),
      el('div', { class: 'checkbox' }, isPaid, el('label', {}, 'Paid (counts as worked time)')),
      field('Maximum minutes (optional)', max)),
    actions: [{ label: 'Cancel' }, {
      label: 'Save', class: 'primary', onClick: async (dialog) => {
        const body = {
          name: name.value.trim(), is_paid: isPaid.checked,
          max_minutes: max.value ? Number(max.value) : null,
        };
        try {
          if (type) await api.put(`/api/org/break-types/${type.id}`, body);
          else await api.post('/api/org/break-types', body);
          dialog.close(); toast('Saved.', 'ok'); location.reload();
        } catch (error) { errorToast(error); }
      },
    }],
  });
}

/* ---------- Working-time rule parameters (section 16) ---------- */

const RULE_LABELS = {
  wt01_max_avg_weekly_minutes: 'WT-01 maximum average weekly working time (minutes)',
  wt01_reference_months: 'WT-01 reference period (months)',
  wt02_min_daily_rest_minutes: 'WT-02 minimum daily rest (minutes)',
  wt03_min_weekly_rest_minutes: 'WT-03 minimum weekly rest (minutes)',
  wt04_break_after_minutes: 'WT-04 break required after (minutes worked)',
  wt04_min_break_minutes: 'WT-04 minimum break length (minutes)',
  wt05_night_avg_max_minutes: 'WT-05 night work limit, average per day (minutes)',
  wt05_reference_months: 'WT-05 reference period (months)',
  wt07_annual_leave_weeks: 'WT-07 annual paid leave minimum (weeks)',
  daily_max_minutes: 'Daily maximum working time (minutes)',
};

async function rulesPanel() {
  const data = await api.get('/api/org/rule-params');
  const effective = el('input', { type: 'date', value: today() });
  const inputs = {};
  const rows = Object.entries(RULE_LABELS).map(([key, label]) => {
    inputs[key] = el('input', { type: 'number', value: String(data.params[key]) });
    return el('tr', {},
      el('th', { scope: 'row', style: 'font-weight:500;white-space:normal' }, label),
      el('td', {}, inputs[key]),
      el('td', { class: 'num hint' }, `default ${data.defaults[key]}`));
  });

  return el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Working-time rules')),
    el('p', { class: 'hint' },
      'National law is frequently stricter than the Directive, so every threshold '
      + 'is a parameter. Parameters are versioned: a historic evaluation keeps the '
      + 'values that were in force at the time.'),
    el('div', { class: 'row' }, field('Effective from', effective)),
    el('div', { class: 'table-wrap' }, el('table', {}, el('tbody', {}, rows))),
    el('button', {
      class: 'primary', style: 'margin-top:.75rem',
      onClick: async () => {
        const params = {};
        for (const [key, input] of Object.entries(inputs)) params[key] = Number(input.value);
        try {
          await api.put('/api/org/rule-params',
            { effective_from: effective.value, params });
          toast('Rule parameters saved as a new version.', 'ok');
        } catch (error) { errorToast(error); }
      },
    }, 'Save as a new version'));
}

/* ---------- Kiosks ---------- */

async function kiosksPanel() {
  const [kiosks, users, locations] = await Promise.all([
    api.get('/api/kiosks'), api.get('/api/users'), api.get('/api/org/locations'),
  ]);
  const card = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Kiosks'),
      el('div', { class: 'spacer' }),
      el('button', {
        class: 'primary', onClick: () => kioskDialog(null, users, locations),
      }, 'Create kiosk')),
    el('p', { class: 'hint' },
      'A kiosk is launched with a unique, revocable link and shows nothing but '
      + 'the roster and the clock action.'));

  if (!kiosks.length) {
    card.appendChild(el('div', { class: 'empty' }, 'No kiosks yet.'));
    return card;
  }
  const table = el('table', {},
    el('thead', {}, el('tr', {}, el('th', {}, 'Kiosk'), el('th', {}, 'Site'),
      el('th', { class: 'num' }, 'Assignees'), el('th', {}, 'Auth'),
      el('th', {}, 'Expires'), el('th', {}, 'Link'), el('th', {}, ''))),
    el('tbody', {}, kiosks.map((kiosk) => el('tr', {},
      el('td', {}, kiosk.name),
      el('td', {}, locations.find((l) => l.id === kiosk.location_id)?.name || '—'),
      el('td', { class: 'num' }, String((kiosk.assignee_ids || []).length)),
      el('td', {}, kiosk.auth_method),
      el('td', {}, kiosk.token_expires_at
        ? new Date(kiosk.token_expires_at + 'Z').toLocaleString() : '—'),
      el('td', {}, kiosk.revoked
        ? el('span', { class: 'badge err' }, 'revoked')
        : el('a', { href: kiosk.launch_url, target: '_blank', rel: 'noopener' }, 'launch')),
      el('td', {},
        el('button', {
          class: 'small', onClick: () => kioskDialog(kiosk, users, locations),
        }, 'Edit'),
        el('button', {
          class: 'small',
          onClick: async () => {
            const result = await api.post(`/api/kiosks/${kiosk.id}/relaunch`);
            modal({
              title: 'New launch link',
              body: el('div', {},
                el('p', {}, 'The previous link stopped working immediately.'),
                el('code', { style: 'word-break:break-all' },
                  location.origin + result.launch_url)),
              actions: [{ label: 'Close', class: 'primary' }],
            });
          },
        }, 'Re-launch'),
        el('button', {
          class: 'small',
          onClick: async () => {
            if (!await confirmDialog('Revoke kiosk',
              'The link stops working immediately.')) return;
            await api.post(`/api/kiosks/${kiosk.id}/revoke`);
            toast('Revoked.', 'ok'); location.reload();
          },
        }, 'Revoke'))))));
  card.appendChild(el('div', { class: 'table-wrap' }, table));
  return card;
}

function kioskDialog(kiosk, users, locations) {
  const name = el('input', { value: kiosk?.name || '' });
  const locationSelect = el('select', {}, el('option', { value: '' }, '—'),
    locations.map((l) => el('option', { value: l.id, selected: kiosk?.location_id === l.id }, l.name)));
  const auth = el('select', {}, ['pin4', 'pin6', 'qr'].map((m) =>
    el('option', { value: m, selected: kiosk?.auth_method === m }, m)));
  const breaks = el('input', { type: 'checkbox', checked: kiosk ? kiosk.breaks_enabled : true });
  const hours = el('input', { type: 'number', value: kiosk?.session_hours ?? 24 });
  const photo = el('input', { type: 'checkbox', checked: kiosk?.require_photo || false });
  const assignees = users.map((user) => {
    const input = el('input', {
      type: 'checkbox', dataset: { user: user.id },
      checked: (kiosk?.assignee_ids || []).includes(user.id),
    });
    return el('div', { class: 'checkbox' }, input, el('label', {}, user.name));
  });

  modal({
    title: kiosk ? `Edit ${kiosk.name}` : 'Create kiosk',
    body: el('div', {},
      el('div', { class: 'row' }, field('Name', name), field('Site', locationSelect)),
      el('div', { class: 'row' }, field('Authentication', auth),
        field('Session length (hours)', hours),
        el('div', { class: 'checkbox' }, breaks, el('label', {}, 'Allow breaks'))),
      el('div', { class: 'checkbox' }, photo,
        el('label', {}, 'Require a photo at clock-in')),
      el('p', { class: 'hint' },
        'Photo capture increases monitoring intensity: it needs a documented legal '
        + 'basis, a works-council agreement and a repeat of the DPIA before it is '
        + 'switched on.'),
      el('fieldset', {}, el('legend', {}, 'Assigned employees'), assignees)),
    actions: [{ label: 'Cancel' }, {
      label: 'Save', class: 'primary', onClick: async (dialog) => {
        const body = {
          name: name.value.trim(),
          location_id: locationSelect.value || null,
          assignee_ids: assignees.map((n) => n.querySelector('input'))
            .filter((i) => i.checked).map((i) => i.dataset.user),
          auth_method: auth.value,
          breaks_enabled: breaks.checked,
          session_hours: Number(hours.value || 24),
          require_photo: photo.checked,
        };
        try {
          if (kiosk) await api.put(`/api/kiosks/${kiosk.id}`, body);
          else {
            const result = await api.post('/api/kiosks', body);
            modal({
              title: 'Kiosk created',
              body: el('div', {}, el('p', {}, 'Open this link on the tablet:'),
                el('code', { style: 'word-break:break-all' }, location.origin + result.launch_url)),
              actions: [{ label: 'Close', class: 'primary' }],
            });
          }
          dialog.close(); toast('Saved.', 'ok');
        } catch (error) { errorToast(error); }
      },
    }],
  });
}

/* ---------- Holidays ---------- */

async function holidaysPanel() {
  const year = new Date().getFullYear();
  const rows = await api.get(`/api/org/holidays?year=${year}`);
  const yearInput = el('input', { type: 'number', value: String(year), style: 'width:6rem' });
  const country = el('input', { value: state.user.organisation.country || 'SK', style: 'width:5rem' });

  return el('div', { class: 'card' },
    el('header', {}, el('h2', {}, `Public holidays ${year}`),
      el('div', { class: 'spacer' }),
      yearInput, country,
      el('button', {
        class: 'primary',
        onClick: async () => {
          try {
            const result = await api.post('/api/org/holidays/import',
              { year: Number(yearInput.value), country: country.value, location_id: null });
            toast(`Imported ${result.created} holiday(s).`, 'ok');
            location.reload();
          } catch (error) { errorToast(error); }
        },
      }, 'Import')),
    rows.length ? el('div', { class: 'table-wrap' },
      el('table', {},
        el('thead', {}, el('tr', {}, el('th', {}, 'Date'), el('th', {}, 'Name'),
          el('th', {}, 'Scope'), el('th', {}, ''))),
        el('tbody', {}, rows.map((row) => el('tr', {},
          el('td', {}, row.day),
          el('td', {}, row.name),
          el('td', {}, row.location_id ? 'site-specific' : 'all sites'),
          el('td', {}, el('button', {
            class: 'small',
            onClick: async () => {
              await api.del(`/api/org/holidays/${row.id}`);
              toast('Removed.', 'ok'); location.reload();
            },
          }, 'Remove')))))))
      : el('div', { class: 'empty' }, 'No holidays loaded for this year.'));
}

/* ---------- Workspace settings ---------- */

async function settingsPanel() {
  const org = await api.get('/api/org');
  const inputs = {};
  const text = (key, label, type = 'text') => {
    inputs[key] = el('input', { type, value: org[key] ?? '' });
    return field(label, inputs[key]);
  };
  const select = (key, label, options) => {
    inputs[key] = el('select', {}, options.map((option) =>
      el('option', { value: option, selected: String(org[key]) === String(option) }, String(option))));
    return field(label, inputs[key]);
  };
  const check = (key, label) => {
    inputs[key] = el('input', { type: 'checkbox', checked: !!org[key] });
    return el('div', { class: 'checkbox' }, inputs[key], el('label', {}, label));
  };

  return el('div', {},
    el('div', { class: 'card' },
      el('header', {}, el('h2', {}, 'Workspace')),
      el('div', { class: 'row' },
        text('name', 'Organisation name'), text('country', 'Country'),
        text('timezone', 'Default time zone')),
      el('div', { class: 'row' },
        select('week_start', 'Week starts on', [0, 1, 2, 3, 4, 5, 6]),
        select('date_format', 'Date format', ['DD/MM/YYYY', 'YYYY-MM-DD', 'MM/DD/YYYY']),
        select('time_format', 'Time format', ['24h', '12h']),
        select('duration_format', 'Duration display', ['hm', 'decimal'])),
      el('div', { class: 'row' },
        select('period_type', 'Attendance period',
          ['weekly', 'biweekly', 'semimonthly', 'monthly']),
        text('submission_cutoff_days', 'Cut-off, days after period end', 'number'),
        text('auto_approve_after_days', 'Auto-approve after (0 = never)', 'number'),
        text('retention_years', 'Retention (years)', 'number'))),

    el('div', { class: 'card' },
      el('header', {}, el('h2', {}, 'Capture channels')),
      el('div', { class: 'row' },
        check('channel_timer', 'Web timer'), check('channel_manual', 'Manual entry'),
        check('channel_grid', 'Weekly grid'), check('channel_kiosk', 'Kiosk'),
        check('channel_mobile', 'Mobile')),
      el('div', { class: 'row' },
        check('require_cost_centre', 'Cost centre mandatory'),
        check('require_note', 'Note mandatory'),
        check('managers_may_launch_kiosk', 'Managers may launch a kiosk'))),

    el('div', { class: 'card' },
      el('header', {}, el('h2', {}, 'Rounding and session limits')),
      el('div', { class: 'row' },
        select('rounding_minutes', 'Round clock-in/out to', [0, 1, 5, 10, 15]),
        select('rounding_direction', 'Rounding direction', ['nearest', 'up', 'down']),
        text('max_session_hours', 'Maximum session (hours)', 'number'),
        check('auto_stop_runaway', 'Auto-stop a runaway session')),
      el('div', { class: 'row' },
        text('auto_break_after_minutes', 'Auto-deduct a break after (minutes)', 'number'),
        text('auto_break_minutes', 'Auto-deducted break (minutes)', 'number')),
      el('p', { class: 'hint' },
        'Rounding is applied at clock-in and clock-out only, never to computed '
        + 'totals, and in the same direction for both events. "Nearest" is '
        + 'symmetric; "up" and "down" should only be used where a collective '
        + 'agreement requires them.')),

    el('button', {
      class: 'primary big',
      onClick: async () => {
        const body = {};
        for (const [key, node] of Object.entries(inputs)) {
          if (node.type === 'checkbox') body[key] = node.checked;
          else if (node.type === 'number') body[key] = Number(node.value);
          else if (['week_start', 'rounding_minutes'].includes(key)) body[key] = Number(node.value);
          else body[key] = node.value;
        }
        try {
          await api.put('/api/org', body);
          toast('Workspace settings saved.', 'ok');
          setTimeout(() => location.reload(), 800);
        } catch (error) { errorToast(error); }
      },
    }, 'Save settings'));
}

/* ---------- Integrations ---------- */

async function integrationsPanel() {
  const [keys, hooks] = await Promise.all([
    api.get('/api/integrations/api-keys'),
    api.get('/api/integrations/webhooks'),
  ]);

  const keyCard = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'API keys'),
      el('div', { class: 'spacer' }),
      el('button', {
        class: 'primary',
        onClick: async () => {
          const name = await promptDialog('New API key', 'What is it for?', { textarea: false });
          if (!name) return;
          try {
            const result = await api.post('/api/integrations/api-keys',
              { name, scopes: ['*'] });
            modal({
              title: 'API key created',
              body: el('div', {},
                el('code', { style: 'word-break:break-all;display:block;padding:.5rem;background:#eef2f6' },
                  result.api_key),
                el('p', { class: 'msg warn' }, result.notice)),
              actions: [{ label: 'Done', class: 'primary' }],
            });
          } catch (error) { errorToast(error); }
        },
      }, 'Create key')),
    el('div', { class: 'table-wrap' },
      el('table', {},
        el('thead', {}, el('tr', {}, el('th', {}, 'Name'), el('th', {}, 'Prefix'),
          el('th', {}, 'Scopes'), el('th', {}, 'Last used'), el('th', {}, ''))),
        el('tbody', {}, keys.map((key) => el('tr', {},
          el('td', {}, key.name),
          el('td', { style: 'font-family:var(--mono)' }, key.prefix),
          el('td', {}, (key.scopes || []).join(', ')),
          el('td', {}, key.last_used_at ? new Date(key.last_used_at + 'Z').toLocaleString() : 'never'),
          el('td', {}, key.revoked ? el('span', { class: 'badge err' }, 'revoked')
            : el('button', {
              class: 'small',
              onClick: async () => {
                await api.post(`/api/integrations/api-keys/${key.id}/revoke`);
                toast('Revoked.', 'ok'); location.reload();
              },
            }, 'Revoke'))))))));

  const hookCard = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Webhooks'),
      el('div', { class: 'spacer' }),
      el('button', {
        class: 'primary',
        onClick: async () => {
          const url = await promptDialog('New webhook', 'Endpoint URL', { textarea: false });
          if (!url) return;
          try {
            const result = await api.post('/api/integrations/webhooks',
              { url, events: hooks.available_events });
            modal({
              title: 'Webhook created',
              body: el('div', {}, el('p', {}, 'Signing secret:'),
                el('code', { style: 'word-break:break-all' }, result.secret),
                el('p', { class: 'hint' }, result.notice)),
              actions: [{ label: 'Done', class: 'primary' }],
            });
          } catch (error) { errorToast(error); }
        },
      }, 'Add webhook')),
    el('p', { class: 'hint' }, `Events: ${hooks.available_events.join(', ')}`),
    hooks.webhooks.length ? el('div', { class: 'table-wrap' },
      el('table', {},
        el('thead', {}, el('tr', {}, el('th', {}, 'URL'), el('th', {}, 'Events'),
          el('th', {}, 'Active'), el('th', {}, ''))),
        el('tbody', {}, hooks.webhooks.map((hook) => el('tr', {},
          el('td', { class: 'wrap' }, hook.url),
          el('td', { class: 'wrap' }, (hook.events || []).length),
          el('td', {}, hook.active ? 'yes' : 'no'),
          el('td', {}, el('button', {
            class: 'small',
            onClick: async () => {
              await api.del(`/api/integrations/webhooks/${hook.id}`);
              toast('Disabled.', 'ok'); location.reload();
            },
          }, 'Disable')))))))
      : el('div', { class: 'empty' }, 'No webhooks configured.'));

  const feedCard = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Other integration points')),
    el('ul', {},
      el('li', {}, 'Calendar feed of approved absence: ',
        el('code', {}, '/api/integrations/calendar.ics')),
      el('li', {}, 'Public API: ', el('code', {}, '/api/v1/employees, /api/v1/entries, '
        + '/api/v1/absences, /api/v1/reports/{type}')),
      el('li', {}, 'OpenAPI description: ', el('a', { href: '/docs', target: '_blank' }, '/docs')),
      el('li', {}, 'SSO service-provider metadata: ',
        el('code', {}, '/api/integrations/sso/metadata')),
      el('li', {}, 'SCIM v2 users: ', el('code', {}, '/api/integrations/scim/v2/Users'))));

  return el('div', {}, keyCard, hookCard, feedCard);
}
