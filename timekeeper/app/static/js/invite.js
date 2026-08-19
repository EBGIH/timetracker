/* Invitation acceptance (FR-B-03). */

import { api, el, errorToast, toast } from './api.js';

const token = location.pathname.split('/').pop();
const root = document.getElementById('root');

async function start() {
  let invitation;
  try {
    invitation = await api.get(`/api/auth/invitation/${token}`, { allowAnonymous: true });
  } catch (error) {
    root.appendChild(el('div', { class: 'card' },
      el('h1', {}, 'This invitation is not valid'),
      el('p', {}, 'It may have expired or already been used. Ask your administrator to send a new one.')));
    return;
  }

  const password = el('input', { type: 'password', autocomplete: 'new-password', required: true, style: 'width:100%' });
  const repeat = el('input', { type: 'password', autocomplete: 'new-password', required: true, style: 'width:100%' });
  const form = el('form', { class: 'card' },
    el('h1', {}, 'Welcome to TimeKeeper'),
    el('p', {}, `Set a password for ${invitation.name} (${invitation.email}).`),
    el('div', { class: 'field' }, el('label', {}, 'Password (at least 10 characters)'), password),
    el('div', { class: 'field' }, el('label', {}, 'Repeat password'), repeat),
    el('button', { class: 'primary big', type: 'submit', style: 'width:100%' }, 'Set password'));

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (password.value !== repeat.value) {
      toast('The two passwords do not match.', 'err');
      return;
    }
    try {
      await api.post(`/api/auth/invitation/${token}`,
        { new_password: password.value }, { allowAnonymous: true });
      root.replaceChildren(el('div', { class: 'card' },
        el('h1', {}, 'You are all set'),
        el('p', {}, 'Your password has been saved.'),
        el('a', { class: 'btn primary', href: '/' }, 'Sign in')));
    } catch (error) { errorToast(error); }
  });

  root.appendChild(form);
}

start();
