/* Application shell: sign-in, navigation and the hash router. */

import {
  api, can, clear, el, errorToast, loadSession, signIn, signOut, state, toast,
} from './api.js';
import { renderTracker } from './views/tracker.js';
import { renderMyDashboard, renderManagerDashboard } from './views/dashboard.js';
import { renderApprovals } from './views/approvals.js';
import { renderAbsence } from './views/absence.js';
import { renderReports } from './views/reports.js';
import { renderAdmin } from './views/admin.js';
import { renderAudit } from './views/audit.js';
import { renderProfile } from './views/profile.js';

const ROUTES = {
  home: { title: 'My dashboard', render: renderMyDashboard },
  tracker: { title: 'Tracker', render: renderTracker },
  manager: { title: 'Team', render: renderManagerDashboard, needs: 'view_team_attendance' },
  approvals: { title: 'Approvals', render: renderApprovals, needs: 'approve_timesheet' },
  absence: { title: 'Absence', render: renderAbsence },
  reports: { title: 'Reports', render: renderReports },
  admin: { title: 'Administration', render: renderAdmin, needs: 'manage_users' },
  audit: { title: 'Audit log', render: renderAudit, needs: 'view_audit' },
  profile: { title: 'My profile', render: renderProfile },
};

const NAV = [
  { group: 'Me', items: ['home', 'tracker', 'absence'] },
  { group: 'Team', items: ['manager', 'approvals'] },
  { group: 'Organisation', items: ['reports', 'admin', 'audit'] },
  { group: 'Account', items: ['profile'] },
];

const root = document.getElementById('root');

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, query] = raw.split('?');
  return { path: path || 'home', params: new URLSearchParams(query || '') };
}

/* ---------- Sign-in ---------- */

function renderLogin(message) {
  clear(root);
  const form = el('form', { class: 'card login-card', novalidate: true });
  const mfaField = el('div', { class: 'field', style: 'display:none' },
    el('label', { for: 'mfa' }, 'Authentication code'),
    el('input', { id: 'mfa', name: 'mfa', inputmode: 'numeric', autocomplete: 'one-time-code' }));

  form.append(
    el('h1', {}, 'TimeKeeper'),
    el('p', { class: 'hint' }, 'Attendance and time tracking'),
    message ? el('div', { class: 'msg err', role: 'alert' }, message) : null,
    el('div', { class: 'field' },
      el('label', { for: 'email' }, 'Work e-mail'),
      el('input', { id: 'email', type: 'email', required: true, autocomplete: 'username', autofocus: true })),
    el('div', { class: 'field' },
      el('label', { for: 'password' }, 'Password'),
      el('input', { id: 'password', type: 'password', required: true, autocomplete: 'current-password' })),
    mfaField,
    el('button', { class: 'primary big', type: 'submit', style: 'width:100%' }, 'Sign in'),
    el('p', { class: 'hint', style: 'margin-top:1rem' },
      'Clocking in at a shared terminal? Use the kiosk link your supervisor gave you.'),
  );

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const email = form.querySelector('#email').value.trim();
    const password = form.querySelector('#password').value;
    const mfa = form.querySelector('#mfa').value.trim();
    try {
      await signIn(email, password, mfa);
      location.hash = '#/home';
      boot();
    } catch (error) {
      if (String(error.message).includes('MFA')) {
        mfaField.style.display = '';
        form.querySelector('#mfa').focus();
        renderLoginError(form, 'Enter the six-digit code from your authenticator app.');
      } else {
        renderLoginError(form, error.message);
      }
    }
  });

  root.appendChild(el('div', { class: 'login-wrap' }, form));
}

function renderLoginError(form, message) {
  form.querySelectorAll('.msg').forEach((n) => n.remove());
  form.insertBefore(el('div', { class: 'msg err', role: 'alert' }, message),
    form.children[2]);
}

/* ---------- Shell ---------- */

function renderShell() {
  clear(root);
  const sidebar = el('aside', { class: 'sidebar', id: 'sidebar' });
  sidebar.append(
    el('div', { class: 'brand' }, 'TimeKeeper', el('span', {}, 'v1.0')),
  );
  const nav = el('nav', { 'aria-label': 'Main' });
  for (const section of NAV) {
    const links = section.items.filter((key) => {
      const route = ROUTES[key];
      return route && (!route.needs || can(route.needs));
    });
    if (!links.length) continue;
    nav.appendChild(el('div', { class: 'navgroup' }, section.group));
    for (const key of links) {
      nav.appendChild(el('a', {
        class: 'navlink', href: `#/${key}`, dataset: { route: key },
      }, ROUTES[key].title));
    }
  }
  sidebar.append(nav, el('div', { class: 'whoami' },
    el('strong', {}, state.user.name),
    el('div', { class: 'role' }, state.user.role),
    el('div', {}, state.user.organisation?.name || ''),
    el('button', { class: 'ghost small', style: 'padding-left:0', onClick: () => signOut() }, 'Sign out'),
  ));

  const topbar = el('header', { class: 'topbar' },
    el('button', {
      class: 'menu-toggle', 'aria-label': 'Toggle navigation',
      onClick: () => sidebar.classList.toggle('open'),
    }, '☰'),
    el('h1', { id: 'page-title', style: 'margin:0;font-size:1.15rem' }, ''),
    el('div', { class: 'spacer' }),
    el('div', { id: 'topbar-extra', class: 'row' }),
    el('button', {
      id: 'bell', class: 'ghost', 'aria-label': 'Notifications',
      onClick: showNotifications,
    }, '🔔 ', el('span', { id: 'bell-count', class: 'badge mute' }, '0')),
  );

  const main = el('main', { class: 'main' }, topbar,
    el('div', { class: 'content', id: 'view', tabindex: '-1' }));
  root.append(el('a', { class: 'skip-link', href: '#view' }, 'Skip to content'),
    el('div', { class: 'app' }, sidebar, main));
}

async function showNotifications() {
  const { modal } = await import('./api.js');
  let rows = [];
  try { rows = await api.get('/api/notifications?limit=30'); } catch (e) { errorToast(e); }
  const list = el('div', {});
  if (!rows.length) list.appendChild(el('div', { class: 'empty' }, 'Nothing new.'));
  for (const row of rows) {
    list.appendChild(el('div', { class: 'card', style: 'margin-bottom:.5rem;padding:.6rem .8rem' },
      el('div', { style: 'font-weight:620' }, row.title),
      el('div', { class: 'hint' }, row.body),
      el('div', { class: 'hint' }, new Date(row.created_at + 'Z').toLocaleString()),
    ));
  }
  modal({
    title: 'Notifications',
    body: list,
    actions: [
      {
        label: 'Mark all read', onClick: async (d) => {
          await api.post('/api/notifications/read-all');
          d.close();
          refreshBell();
        },
      },
      { label: 'Close', class: 'primary' },
    ],
  });
}

async function refreshBell() {
  try {
    const rows = await api.get('/api/notifications?unread_only=true&limit=99');
    const badge = document.getElementById('bell-count');
    if (!badge) return;
    badge.textContent = String(rows.length);
    badge.className = rows.length ? 'badge err' : 'badge mute';
  } catch { /* ignore */ }
}

/* ---------- Router ---------- */

async function route() {
  const { path, params } = parseHash();
  const entry = ROUTES[path] || ROUTES.home;
  if (entry.needs && !can(entry.needs)) {
    document.getElementById('view').replaceChildren(
      el('div', { class: 'msg err' }, 'You do not have permission to view this page.'));
    return;
  }
  document.querySelectorAll('.navlink').forEach((link) => {
    if (link.dataset.route === path) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  document.getElementById('page-title').textContent = entry.title;
  document.title = `${entry.title} · TimeKeeper`;
  const view = document.getElementById('view');
  clear(view);
  clear(document.getElementById('topbar-extra'));
  view.appendChild(el('div', { class: 'empty' }, 'Loading…'));
  try {
    const node = await entry.render(params);
    clear(view);
    view.appendChild(node);
  } catch (error) {
    clear(view);
    view.appendChild(el('div', { class: 'msg err' }, error.message || 'Failed to load.'));
    console.error(error);
  }
  document.getElementById('sidebar')?.classList.remove('open');
}

async function boot() {
  const user = await loadSession();
  if (!user) {
    renderLogin();
    return;
  }
  renderShell();
  window.addEventListener('hashchange', route);
  await route();
  refreshBell();
  setInterval(refreshBell, 60000);
}

window.addEventListener('DOMContentLoaded', async () => {
  try {
    const status = await api.get('/api/setup/status', { allowAnonymous: true });
    if (!status.initialised) {
      renderSetup();
      return;
    }
  } catch { /* fall through to sign-in */ }
  boot();
});

/* ---------- First-run setup ---------- */

function renderSetup() {
  clear(root);
  const form = el('form', { class: 'card login-card' });
  form.append(
    el('h1', {}, 'Set up TimeKeeper'),
    el('p', { class: 'hint' }, 'Create the organisation and its owner account.'),
    ...[['organisation', 'Organisation name', 'text'],
    ['first_name', 'Owner first name', 'text'],
    ['last_name', 'Owner last name', 'text'],
    ['email', 'Owner e-mail', 'email'],
    ['password', 'Password (min. 10 characters)', 'password']].map(([id, label, type]) =>
      el('div', { class: 'field' },
        el('label', { for: id }, label),
        el('input', { id, type, required: true, style: 'width:100%' }))),
    el('div', { class: 'field' },
      el('label', { for: 'timezone' }, 'Time zone'),
      el('input', { id: 'timezone', value: Intl.DateTimeFormat().resolvedOptions().timeZone, style: 'width:100%' })),
    el('button', { class: 'primary big', type: 'submit', style: 'width:100%' }, 'Create workspace'),
  );
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const body = {};
    ['organisation', 'first_name', 'last_name', 'email', 'password', 'timezone']
      .forEach((id) => { body[id] = form.querySelector('#' + id).value.trim(); });
    try {
      await api.post('/api/setup', body, { allowAnonymous: true });
      toast('Workspace created — please sign in.', 'ok');
      renderLogin();
    } catch (error) { errorToast(error); }
  });
  root.appendChild(el('div', { class: 'login-wrap' }, form));
}
