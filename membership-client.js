/* Growth Intel membership UI. Paid access is always determined by the API cookie session. */
(() => {
  'use strict';
  const api = '/api/v1/membership';
  const $ = (selector) => document.querySelector(selector);
  const call = async (path, options = {}) => {
    const response = await fetch(api + path, {
      credentials: 'include',
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      ...options
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || 'Request failed');
    return body;
  };
  const message = (text, error = false) => {
    const node = $('#message');
    if (node) { node.textContent = text; node.className = error ? 'message error' : 'message'; }
  };
  const show = (id, visible = true) => { const node = $(id); if (node) node.hidden = !visible; };

  async function loadMember() {
    try {
      const user = await call('/me');
      show('#auth', false); show('#member');
      $('#identity').textContent = `${user.name} · ${user.membership_state}`;
      $('#reference').textContent = user.gi_reference;
      const bank = await call('/bank-transfer');
      for (const key of ['account_name', 'sort_code', 'account_number', 'reference', 'amount_display']) {
        const node = document.querySelector(`[data-value="${key}"]`);
        if (node) node.textContent = bank[key];
      }
      show('#standing-order', bank.standing_order_available);
      if (user.is_admin) show('#admin-link');
    } catch (_) { show('#auth'); show('#member', false); }
  }

  document.addEventListener('click', async (event) => {
    const copy = event.target.closest('[data-copy]');
    if (copy) {
      const value = document.querySelector(`[data-value="${copy.dataset.copy}"]`)?.textContent || '';
      await navigator.clipboard.writeText(value); copy.textContent = 'Copied'; setTimeout(() => { copy.textContent = 'Copy'; }, 1200);
    }
  });
  $('#register')?.addEventListener('submit', async (event) => {
    event.preventDefault(); const values = Object.fromEntries(new FormData(event.target));
    try { await call('/register', { method: 'POST', body: JSON.stringify(values) }); message('Account created. Sign in to continue.'); event.target.reset(); }
    catch (error) { message(error.message, true); }
  });
  $('#login')?.addEventListener('submit', async (event) => {
    event.preventDefault(); const values = Object.fromEntries(new FormData(event.target));
    try { await call('/login', { method: 'POST', body: JSON.stringify(values) }); message('Signed in securely.'); await loadMember(); }
    catch (error) { message(error.message, true); }
  });
  $('#sent-payment')?.addEventListener('click', async (event) => {
    try { const result = await call('/payment-requests', { method: 'POST', body: '{}' }); message(result.message); event.target.disabled = true; }
    catch (error) { message(error.message, true); }
  });
  $('#logout')?.addEventListener('click', async () => { await call('/logout', { method: 'POST', body: '{}' }); location.reload(); });
  loadMember();
})();
