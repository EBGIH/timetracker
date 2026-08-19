/* Personal settings: password, MFA, notification preferences, data export. */

import { api, el, errorToast, modal, state, toast } from '../api.js';

export async function renderProfile() {
  const catalogue = await api.get('/api/notifications/catalogue');
  const wrap = el('div');

  wrap.appendChild(el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Account')),
    el('div', { class: 'grid cols-3' },
      info('Name', state.user.name),
      info('Personnel number', state.user.personnel_number),
      info('Role', state.user.role),
      info('E-mail', state.user.email || '—'),
      info('Organisation', state.user.organisation?.name || '—'),
      info('Time zone', state.user.organisation?.timezone || '—'))));

  /* Password */
  const current = el('input', { type: 'password', autocomplete: 'current-password' });
  const next = el('input', { type: 'password', autocomplete: 'new-password' });
  wrap.appendChild(el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Password')),
    el('div', { class: 'row' },
      el('div', { class: 'field' }, el('label', {}, 'Current password'), current),
      el('div', { class: 'field' }, el('label', {}, 'New password (min. 10 characters)'), next),
      el('button', {
        class: 'primary',
        onClick: async () => {
          try {
            await api.post('/api/auth/password',
              { current_password: current.value, new_password: next.value });
            current.value = ''; next.value = '';
            toast('Password changed.', 'ok');
          } catch (error) { errorToast(error); }
        },
      }, 'Change password'))));

  /* MFA */
  const mfaCard = el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Two-factor authentication'),
      el('span', { class: state.user.mfa_enabled ? 'badge ok' : 'badge warn' },
        state.user.mfa_enabled ? 'enabled' : 'not enabled')),
    el('p', { class: 'hint' },
      state.user.mfa_required
        ? 'Multi-factor authentication is mandatory for your role.'
        : 'Optional for your role, but recommended.'));
  if (!state.user.mfa_enabled) {
    mfaCard.appendChild(el('button', {
      class: 'primary',
      onClick: async () => {
        try {
          const enrol = await api.post('/api/auth/mfa/enrol');
          const code = el('input', { inputmode: 'numeric', placeholder: '123456' });
          modal({
            title: 'Set up your authenticator',
            body: el('div', {},
              el('p', {}, 'Add this secret to your authenticator app:'),
              el('code', {
                style: 'display:block;padding:.5rem;background:#eef2f6;word-break:break-all',
              }, enrol.secret),
              el('p', { class: 'hint', style: 'word-break:break-all' }, enrol.uri),
              el('div', { class: 'field' },
                el('label', {}, 'Enter the current six-digit code'), code)),
            actions: [{ label: 'Cancel' }, {
              label: 'Confirm', class: 'primary', onClick: async (dialog) => {
                try {
                  await api.post('/api/auth/mfa/confirm', { code: code.value });
                  dialog.close();
                  toast('Two-factor authentication enabled.', 'ok');
                  setTimeout(() => location.reload(), 800);
                } catch (error) { errorToast(error); }
              },
            }],
          });
        } catch (error) { errorToast(error); }
      },
    }, 'Set up'));
  } else if (!state.user.mfa_required) {
    mfaCard.appendChild(el('button', {
      onClick: async () => {
        try {
          await api.post('/api/auth/mfa/disable');
          toast('Disabled.', 'ok');
          setTimeout(() => location.reload(), 600);
        } catch (error) { errorToast(error); }
      },
    }, 'Disable'));
  }
  wrap.appendChild(mfaCard);

  /* Notification preferences (FR-K-03) */
  const prefs = {};
  const rows = catalogue.map((entry) => {
    const input = el('input', {
      type: 'checkbox', checked: true, disabled: !entry.optional,
      dataset: { type: entry.type },
    });
    prefs[entry.type] = input;
    return el('tr', {},
      el('td', {}, entry.type.replace(/_/g, ' ')),
      el('td', {}, entry.default_channels.join(', ')),
      el('td', {}, entry.optional
        ? el('div', { class: 'checkbox' }, input, el('label', {}, 'e-mail as well as in-app'))
        : el('span', { class: 'badge mute' }, 'always on')));
  });
  wrap.appendChild(el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Notifications')),
    el('p', { class: 'hint' },
      'A minimum mandatory set cannot be switched off — you will always be told '
      + 'when someone amends one of your records or decides one of your requests.'),
    el('div', { class: 'table-wrap' },
      el('table', {},
        el('thead', {}, el('tr', {}, el('th', {}, 'Event'),
          el('th', {}, 'Default channels'), el('th', {}, 'Your choice'))),
        el('tbody', {}, rows))),
    el('button', {
      class: 'primary', style: 'margin-top:.75rem',
      onClick: async () => {
        const body = {};
        for (const [type, input] of Object.entries(prefs)) {
          if (!input.disabled) body[type] = input.checked ? null : false;
        }
        try {
          await api.put('/api/auth/notification-prefs', { prefs: body });
          toast('Preferences saved.', 'ok');
        } catch (error) { errorToast(error); }
      },
    }, 'Save preferences')));

  /* Data rights */
  wrap.appendChild(el('div', { class: 'card' },
    el('header', {}, el('h2', {}, 'Your data')),
    el('p', { class: 'hint' },
      'You can download everything the system holds about you in a '
      + 'machine-readable format at any time.'),
    el('button', {
      onClick: () => api.downloadGet(`/api/users/${state.user.id}/data-export`,
        'my-timekeeper-data.json'),
    }, 'Download my data')));

  return wrap;
}

function info(label, value) {
  return el('div', { class: 'stat' },
    el('div', { class: 'label' }, label),
    el('div', { style: 'font-size:1.05rem;font-weight:600' }, value));
}
