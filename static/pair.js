'use strict';
(() => {
  const code = document.getElementById('pair-code');
  const supplied = new URLSearchParams(location.hash.slice(1)).get('pair');
  // URL fragments are not sent in HTTP requests. Remove it before any request.
  if (location.hash) history.replaceState(null, '', location.pathname);
  if (supplied) code.value = supplied;
  fetch('/api/state').then((response) => {
    if (response.ok) location.replace('/');
  }).catch(() => {});
  document.getElementById('pair-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = document.getElementById('pair-submit');
    const status = document.getElementById('pair-status');
    button.disabled = true;
    status.textContent = 'Verbinde …';
    try {
      const response = await fetch('/api/pair', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({code: code.value.trim()})
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || 'Verbindung fehlgeschlagen.');
      code.value = '';
      location.replace('/');
    } catch (error) {
      status.textContent = error.message || 'Verbindung fehlgeschlagen. Bitte erneut versuchen.';
      button.disabled = false;
    }
  });
})();
