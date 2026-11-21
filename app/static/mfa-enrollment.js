(() => {
  const message = document.querySelector('#message');
  async function send(path, body) {
    const response = await fetch(path, {method:'POST', credentials:'same-origin', cache:'no-store',
      headers:{'Content-Type':'application/json', 'X-CSRF-Token':document.querySelector('#mfa-csrf').value}, body:JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || '绑定失败，请重新登录后再试');
    return result;
  }
  document.querySelector('#begin').addEventListener('click', async event => {
    event.currentTarget.disabled = true;
    try {
      const result = await send('/api/auth/mfa/begin', {});
      document.querySelector('#secret').textContent = result.secret;
      document.querySelector('#codes').textContent = result.recovery_codes.join('\n');
      document.querySelector('#binding').hidden = false;
      message.textContent = '请在10分钟内完成绑定。';
    } catch(error) { message.textContent = error.message; document.querySelector('#begin').disabled = false; }
  });
  document.querySelector('#finish').addEventListener('submit', async event => {
    event.preventDefault(); const form = event.currentTarget, button = form.querySelector('button'); button.disabled = true;
    try {
      const result = await send('/api/auth/mfa/finish', {otp:form.elements.otp.value, recovery_saved:form.elements.saved.checked});
      document.querySelector('#binding').replaceChildren(); message.textContent = result.message;
      location.replace(result.redirect);
    } catch(error) { message.textContent = error.message; } finally { button.disabled = false; }
  });
  window.addEventListener('pagehide', () => {document.querySelector('#binding').replaceChildren();});
  window.addEventListener('pageshow', event => {if (event.persisted) location.reload();});
})();
