/* Setup secrets stay in this page only; no analytics, storage or external requests. */
(() => {
  'use strict';
  const form = document.getElementById('account-setup-form');
  if (!form) return;
  const field = id => document.getElementById(id);
  const username = field('setup-username');
  const password = field('setup-password');
  const confirm = field('setup-confirm');
  const otp = field('setup-otp');
  const saved = field('setup-recovery-saved');
  const error = field('setup-error');
  const begin = field('setup-begin');
  const finish = field('setup-finish');
  const csrf = field('setup-csrf').value;
  let step = 1;
  let busy = false;
  let expiresAt = 0;
  let timer = null;
  let recoveryCodes = [];
  let setupUsername = '';

  const clearError = () => { error.textContent = ''; error.hidden = true; };
  const showError = message => {
    error.textContent = message;
    error.hidden = false;
    error.focus();
  };
  const clearSecrets = () => {
    password.value = '';
    confirm.value = '';
    otp.value = '';
    field('setup-secret').value = '';
    field('setup-recovery-codes').replaceChildren();
    recoveryCodes = [];
    saved.checked = false;
    field('setup-copy-status').textContent = '';
  };
  const expire = () => {
    step = 3;
    clearInterval(timer);
    clearSecrets();
    form.hidden = true;
    field('setup-expired').hidden = false;
    clearError();
    field('setup-restart').focus();
  };
  const updateTime = () => {
    const left = Math.ceil((expiresAt - Date.now()) / 1000);
    if (left <= 0) return expire();
    field('setup-expiry').textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')} 内完成`;
  };
  const request = async (url, body) => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(url, {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: { 'Content-Type': 'application/json', 'X-Setup-CSRF': csrf },
        body: JSON.stringify(body), signal: controller.signal
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '设置未完成，请检查填写内容后重试。');
      return result;
    } finally { clearTimeout(timeout); }
  };
  const validateAccount = () => {
    const name = username.value.trim();
    if (!name) throw new Error('请填写管理员账号。');
    if (password.value !== confirm.value) throw new Error('两次输入的密码不一致，请重新确认。');
    if (password.value.length < 12 || password.value.length > 1024 || !/[A-Z]/.test(password.value) || !/[a-z]/.test(password.value) || !/[0-9]/.test(password.value) || !/[^\p{L}\p{N}\s]/u.test(password.value)) {
      throw new Error('密码须为 12 至 1024 个字符，包含大写、小写字母、数字和特殊字符。');
    }
    if (password.value.toLocaleLowerCase().includes(name.toLocaleLowerCase())) throw new Error('密码不能包含管理员账号。');
    return name;
  };
  const showBinding = result => {
    if (typeof result.secret !== 'string' || !result.secret || !Array.isArray(result.recovery_codes) || !result.recovery_codes.length || result.recovery_codes.some(code => typeof code !== 'string')) {
      throw new Error('未能取得完整绑定信息，请刷新页面重新开始。');
    }
    recoveryCodes = result.recovery_codes.slice();
    field('setup-secret').value = result.secret;
    field('binding-account').textContent = `织造台 · ${setupUsername}`;
    const items = recoveryCodes.map(code => {
      const item = document.createElement('li');
      item.textContent = code;
      return item;
    });
    field('setup-recovery-codes').replaceChildren(...items);
    field('setup-account-fields').hidden = true;
    field('setup-mfa-fields').hidden = false;
    for (const input of [username, password, confirm]) input.readOnly = true;
    otp.required = true;
    saved.required = true;
    field('setup-step-account').className = 'is-complete';
    field('setup-step-account').removeAttribute('aria-current');
    field('setup-step-mfa').className = 'is-current';
    field('setup-step-mfa').setAttribute('aria-current', 'step');
    const seconds = Number.isFinite(result.expires_in) && result.expires_in > 0 ? Math.min(result.expires_in, 600) : 600;
    expiresAt = Date.now() + seconds * 1000;
    step = 2;
    updateTime();
    timer = setInterval(updateTime, 1000);
    field('setup-secret').focus();
  };

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || step === 3) return;
    clearError();
    if (!form.reportValidity()) return;
    busy = true;
    begin.disabled = true;
    finish.disabled = true;
    form.setAttribute('aria-busy', 'true');
    try {
      if (step === 1) {
        setupUsername = validateAccount();
        showBinding(await request('/commerce-setup/begin', { username: setupUsername }));
      } else {
        if (Date.now() >= expiresAt) return expire();
        if (!saved.checked) throw new Error('请先妥善保存恢复码，并勾选确认。');
        if (!/^[0-9]{6}$/.test(otp.value)) throw new Error('请输入验证器中的 6 位动态验证码。');
        const result = await request('/commerce-setup/finish', {
          username: setupUsername, password: password.value, confirm_password: confirm.value,
          otp: otp.value, recovery_saved: true
        });
        clearInterval(timer);
        clearSecrets();
        // The API returns this fixed local route; never follow arbitrary URLs.
        const target = typeof result.redirect === 'string' ? result.redirect : '/commerce-login?setup=complete';
        const destination = new URL(target, location.origin);
        if (destination.origin !== location.origin || destination.pathname !== '/commerce-login') {
          location.replace('/commerce-login?setup=complete');
        } else {
          location.replace(destination.pathname + destination.search);
        }
      }
    } catch (exception) {
      if (step !== 3) showError(exception.name === 'AbortError' ? (step === 1 ? '绑定信息请求超时，请刷新页面重新开始。' : '请求超时。设置可能已完成，请先使用页面下方入口尝试登录；若未完成，可刷新页面重新开始。') : (exception instanceof TypeError ? '连接失败，请检查网络后重试。' : exception.message));
    } finally {
      busy = false;
      begin.disabled = false;
      finish.disabled = false;
      form.removeAttribute('aria-busy');
    }
  });
  const copy = async value => {
    if (!value || step !== 2) return;
    try {
      if (!navigator.clipboard || !window.isSecureContext) throw new Error();
      await navigator.clipboard.writeText(value);
      field('setup-copy-status').textContent = '已复制，请粘贴到你选择的安全位置。';
    } catch (_) {
      field('setup-copy-status').textContent = '当前浏览器无法自动复制，请手动选取并保存。';
    }
  };
  field('copy-secret').addEventListener('click', () => copy(field('setup-secret').value));
  field('copy-recovery').addEventListener('click', () => copy(recoveryCodes.join('\n')));
  field('setup-restart').addEventListener('click', () => location.reload());
  document.addEventListener('visibilitychange', () => { if (step === 2) updateTime(); });
  window.addEventListener('pagehide', () => { clearInterval(timer); clearSecrets(); });
  window.addEventListener('pageshow', event => { if (event.persisted) location.reload(); });
})();
