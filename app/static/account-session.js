/* Visible account access in the workspace; authentication remains server-owned. */
(() => {
  'use strict';
  const surfaces = [...document.querySelectorAll('[data-account-session]')];
  if (!surfaces.length) return;
  const roles = { admin: '超级管理员', shareholder: '股东', operator: '运营', production: '生产' };
  const link = (text, href) => {
    const node = document.createElement('a');
    node.textContent = text;
    node.href = href;
    return node;
  };
  const render = (surface, state) => {
    surface.replaceChildren();
    if (state.authenticated) {
      const badge = document.createElement('span');
      badge.className = 'workspace-account-badge';
      const dot = document.createElement('span');
      dot.className = 'workspace-account-dot';
      dot.setAttribute('aria-hidden', 'true');
      const name = document.createElement('span');
      name.className = 'workspace-account-name';
      name.textContent = state.username || '已登录';
      const role = document.createElement('span');
      role.className = 'workspace-account-role';
      role.textContent = roles[state.role] || '成员';
      badge.append(dot, name, role);
      surface.append(badge, link('更改密码', state.role==='shareholder' ? '/shareholder-password' : '/commerce-password'));
      if(state.role==='admin' && state.password_only)surface.append(link('用户管理','/user-management'));
      const form = document.createElement('form');
      form.method = 'post';
      form.action = '/commerce-logout';
      const button = document.createElement('button');
      button.type = 'submit';
      button.textContent = '退出登录';
      form.append(button);
      form.addEventListener('submit', () => { button.disabled = true; button.textContent = '正在退出…'; });
      surface.append(form);
    } else if (state.activation_pending) {
      surface.append(link('设置超级管理员初始密码', '/admin-activate'));
    } else if (state.setup_required) {
      surface.append(link('设置管理员并启用登录', '/commerce-setup'));
    } else {
      surface.append(link('登录工作台', '/commerce-login'));
    }
    if (state.local_only) {
      const hint = document.createElement('span');
      hint.className = 'workspace-account-hint';
      hint.textContent = '仅本机访问';
      surface.append(hint);
    }
    surface.hidden = false;
  };
  const load = async () => {
    try {
      const response = await fetch('/api/auth/session', { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } });
      if (!response.ok) throw new Error();
      const state = await response.json();
      if (!state || typeof state.authenticated !== 'boolean' || typeof state.setup_required !== 'boolean') throw new Error();
      for (const surface of surfaces) render(surface, state);
    } catch (_) {
      for (const surface of surfaces) {
        surface.replaceChildren(link('账号登录', '/commerce-login'));
        const warning = document.createElement('span');
        warning.className = 'workspace-account-error';
        warning.textContent = '暂时无法读取账号状态';
        surface.append(warning);
        surface.hidden = false;
      }
    }
  };
  load();
  window.addEventListener('pageshow', event => { if (event.persisted) load(); });
})();
