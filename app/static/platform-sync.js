const regularRow = row;
row = function renderPlatformAccount(account) {
  if (page !== 'platform_accounts') return regularRow(account);
  const state=account.connection_status||'待配置';
  const auth=`<span class="badge ${state==='已验证'?'':'off'}">${esc(state)}</span>`;
  const syncLabels = {never:'尚未同步', queued:'已提交后台同步', skipped:'已跳过', blocked:'同步受阻', pending_adapter:'等待适配器', success:'同步成功', error:'同步失败'};
  const last = syncLabels[account.last_sync_status] || account.last_sync_status || '尚未同步';
  const cells = [
    `<div class="item-name">${esc(account.account_name)}</div><div class="sub">${esc(account.seller_id || '未填写商家 ID')}</div>`,
    `${esc(account.platform)}<div class="sub">${(account.targets||[]).filter(t=>t.environment==='production').map(t=>`${esc(t.name)} · ${t.enabled?'正式':'正式 · 已停用'}`).join('；') || '尚未关联正式地区'}</div>`,
    `${auth}<div class="sub">${esc(account.credential_source || '点击授权配置填写')}</div>`,
    `<div>${esc(last)}</div><div class="sub">${esc(account.last_sync_at || '—')} · ${esc(account.last_sync_message || '')}</div>`,
  ];
  return `<tr>${cells.map(cell => `<td>${cell}</td>`).join('')}<td><div class="actions"><button onclick="commerceAuthorization(${account.id})">授权配置</button><button onclick="syncPlatformAccount(${account.id})">验证 / 同步</button><button onclick="commerceTarget(${account.id})">选择地区</button><button onclick="commerceLanguages(${account.id})">地区语言</button><button onclick="commerceShipping(${account.id})">配送模板</button><button onclick="commerceGtinEvidence(${account.id})">GTIN 豁免回执</button><button onclick="commerceOpen('commerce_listings')">平台上架</button><button onclick="openEditor(${account.id})">编辑</button><button class="delete" onclick="removeItem(${account.id})">删除</button></div></td></tr>`;
};

async function syncPlatformAccount(accountId = null) {
  notice('正在检查账号并同步平台规则…');
  try {
    const result = await api('/api/sync/run', {method:'POST', body:JSON.stringify(accountId ? {account_id:accountId} : {})});
    const first = result.results?.[0];
    notice(first?.message || `同步任务完成，共处理 ${result.count} 个账号`, first?.status === 'blocked');
    await load();
  } catch (error) {
    notice(error.message, true);
  }
}

window.syncPlatformAccount = syncPlatformAccount;

const regularPlatformRender = render;
render = function renderPlatformSync() {
  regularPlatformRender();
  if (page !== 'platform_accounts') return;
  $('#content').insertAdjacentHTML('afterbegin', '<div class="c-toolbar"><button onclick="commerceTarget()">账号与地区多选</button><button onclick="commerceSyncTargets()">同步所选账号与地区</button></div><p class="c-hint">先新增账号，再点击“授权配置”填写 Seller ID 和应用授权参数，然后选择地区。一个账号可关联多个地区，同一地区可关联多个账号。一键上架按已启用账号地区统一处理已有商品资料。</p>');
};
