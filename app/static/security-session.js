/* Only real interaction extends a secure session; background polling does not. */
(() => {
  // HTTP is accepted only by the explicitly enabled loopback login mode.
  // The session endpoint determines whether this page has an authenticated user.
  let idleMs = 7 * 24 * 60 * 60 * 1000;
  let lastInteraction = Date.now();
  let lastRenewal = lastInteraction;
  let renewing = false;
  let ended = false;
  let authenticated = false;
  const markActivity = event => {
    if (authenticated && event.isTrusted && document.visibilityState === 'visible') {
      if (Date.now() - lastInteraction >= idleMs) return lock();
      lastInteraction = Date.now();
    }
  };
  for (const event of ['pointerdown', 'pointermove', 'keydown', 'wheel', 'touchstart']) {
    document.addEventListener(event, markActivity, { passive: true });
  }
  const lock = () => {
    if (ended) return;
    ended = true;
    // Remove sensitive content immediately, including when network is offline.
    document.body.replaceChildren();
    const message = document.createElement('p');
    message.textContent = '会话已锁定，请重新登录。';
    document.body.append(message);
    location.replace('/commerce-login');
  };
  const tick = async () => {
    if (Date.now() - lastInteraction >= idleMs) return lock();
    if (renewing || ended || document.visibilityState !== 'visible' || lastInteraction <= lastRenewal || Date.now() - lastRenewal < 60000) return;
    renewing = true;
    try {
      const response = await fetch('/commerce-session-activity', { method: 'POST', credentials: 'same-origin' });
      if (response.status === 401 || response.status === 403) return lock();
      if (response.ok) lastRenewal = Date.now();
    } catch (_) { /* Server expiry remains authoritative during disconnection. */ }
    finally { renewing = false; }
  };
  fetch('/api/auth/session', { credentials: 'same-origin', cache: 'no-store' })
    .then(response => response.ok ? response.json() : null)
    .then(state => {
      if (!state?.authenticated) return;
      if (Number.isFinite(state.idle_timeout_seconds) && state.idle_timeout_seconds > 0)
        idleMs = state.idle_timeout_seconds * 1000;
      authenticated = true;
      setInterval(tick, 10000);
      document.addEventListener('visibilitychange', tick);
      window.addEventListener('pageshow', tick);
    }).catch(() => {});
})();
