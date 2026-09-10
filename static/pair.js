'use strict';
(() => {
  const code = document.getElementById('pair-code');
  const remember = document.getElementById('pair-remember');
  const status = document.getElementById('pair-status');
  const supplied = new URLSearchParams(location.hash.slice(1)).get('pair');
  // URL fragments are not sent in HTTP requests. Remove it before any request.
  if (location.hash) history.replaceState(null, '', location.pathname);
  if (supplied) code.value = supplied;
  else {
    status.textContent = 'No pairing code in this link. If you need to sign in again, get a new link from your Mac below.';
    document.getElementById('pair-help').open = true;
  }
  fetch('/api/state').then((response) => {
    if (response.ok) location.replace('/');
  }).catch(() => {});
  document.getElementById('pair-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = document.getElementById('pair-submit');
    button.disabled = true;
    status.textContent = 'Connecting…';
    try {
      const response = await fetch('/api/pair', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({code: code.value.trim(), remember: remember.checked})
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || 'Could not connect.');
      code.value = '';
      location.replace('/');
    } catch (error) {
      status.textContent = error.message || 'Could not connect. Please try again.';
      document.getElementById('pair-help').open = true;
      button.disabled = false;
    }
  });
})();
