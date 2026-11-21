/* Detail pages are displayed inside the product design results. */
(() => {
  const pending = new Set();
  const labels = {queued:'等待开始', running:'处理中', retry_wait:'等待重连', paused:'已暂停', completed:'已完成', superseded:'已替换'};
  const safe = url => /^\/design-outputs\//.test(url || '') ? esc(url) : '';
  const action = (id, type, label, disabled, extra='') => `<button type="button" data-detail-action="${type}" data-detail-id="${id}" ${disabled || pending.has(id) ? 'disabled' : ''} ${extra}>${label}</button>`;
  window.renderDesignTaskLog = (tasks=[], retryPolicy=false) => {
    const names = {master:'内部母稿',story:'整组方案规划',plan:'设计方案',image:'图片生成',recognize:'资料识别',outline:'详情规划'};
    const completed = {master:'母稿已生成',story:'规划已完成',plan:'方案已保存',image:'图片已生成'};
    const kinds = [...new Set(tasks.map(t=>t.kind))];
    return kinds.map(kind=>`<section class="flow-task-group"><h4>${esc(names[kind]||kind||'历史任务')}</h4>${tasks.filter(t=>t.kind===kind).map(t=>{
      const phase=kind==='plan'?'方案':kind==='image'?'生成':'';
      const title=[t.product_name,t.page_label?t.page_label+phase:names[kind],t.task_key].filter(Boolean).join(' · ');
      const state=t.status==='completed'?(completed[kind]||'已完成'):(labels[t.status]||t.status);
      const calls=Number(t.total_attempts??t.attempts??0);
      const model=calls?`${t.model_name?' · '+esc(t.model_name):''}${t.model_role?' · 备用'+esc(t.model_role):' · 主模型'} · ${retryPolicy?'第 '+esc(t.retry_round||1)+'/3 轮 · 当前模型调用 '+esc(t.attempts||0)+' · 累计 '+esc(calls):'历史调用 '+esc(calls)}`:'';
      return `<p data-task-kind="${esc(kind||'')}" data-task-key="${esc(t.task_key||'')}"><b>${esc(title)}</b><br>${esc(state)}${model}${t.error?' · '+esc(t.error):''}</p>`;
    }).join('')}</section>`).join('');
  };
  window.renderDetailPage = (id, state, designBusy) => {
    if (!state) return '';
    const run = state.run, saved = state.current;
    const generated = saved?.verification?.protocol === 'generated-assets-v1' && saved.verification.decision === 'generated';
    const savedChecked = generated || saved?.verification?.protocol === 'image-fidelity-v3' && saved.verification.decision === 'eligible' &&
      ['shape','color','material','parts','count','brand','copy','scene_integration','layout','readability','purpose','prop_scope'].every(key => saved.verification.checks?.[key] === true);
    const active = ['queued','running','retry_wait'].includes(run?.status);
    const busy = designBusy || active || pending.has(id);
    const progress = run && run.status !== 'superseded' ? `<p class="flow-muted">${esc(run.stage_label)} · ${esc(labels[run.status] || run.status)} · ${run.completed}/${run.total}${run.error ? ' · '+esc(run.error) : ''}</p>` : '';
    const whole=Boolean(state.design_contract)||saved?.presentation_protocol==='one-detail-per-sku-v1'||(!run&&!saved);
    if(!run&&!saved)state={...state,design_contract:'three-stage-eight-plans-v2'};
    return `<section class="detail-design-results"><h3>${whole?'每规格完整详情图':'商品长详情页'}</h3>${progress}
      ${saved ? `<p class="flow-muted">${saved.sections.length} ${whole?'张完整详情图':'段'} · 图内语言 ${esc(saved.language)} · ${savedChecked ? (generated ? '已生成，可随生产资料下载' : '历史图片检查通过，可随生产资料下载') : '历史图片保留；需按当前要求重新设计后用于上架'}${state.stale ? ' · 商品资料已变化，请重新设计' : ''}</p>
        <details data-section="detail-long" open><summary>${whole?'各规格完整详情图':'完整长图'}</summary>${whole?(saved.sku_documents||[]).map(d=>`<h4>${esc(d.name)} ${esc(d.color||'')}</h4><a href="${safe(d.url)}" target="_blank" rel="noopener">打开完整详情图</a><div class="detail-continuous"><img ${window.designImage(d.url,960)} alt="${esc(d.name)}完整详情图"></div>`).join(''):`<a href="${safe(saved.url)}" target="_blank" rel="noopener">打开原图</a><div class="detail-continuous"><img ${window.designImage(saved.url,960)} alt="完整商品长详情页"></div>`}</details>
        <details data-section="detail-segments"><summary>${whole?'按规格查看与重做':'分段查看与重做'}</summary><div class="flow-delivery-grid">${saved.sections.map(s => `<article><h4>${esc(s.title)}</h4><a href="${safe(s.url)}" target="_blank" rel="noopener"><img ${window.designImage(s.url)} alt="${esc(s.title)}"></a>${action(id,'detail_single',whole?'重做这张完整详情图':'重做这一段',busy || state.stale,`data-detail-key="${esc(s.page_key)}"`)}</article>`).join('')}</div></details>` : `<p class="flow-muted">${state.design_contract?'完整详情图与本规格7张销售图同轮生成，可从未完成处继续。':'详情页是整组设计的最后一步，完成成品图后会自动继续。已有成品的商品可点击“继续设计”补齐。'}</p>`}
      ${state.candidate ? `<p class="flow-muted">本次详情未完成，已保留候选，原有成品不替换。${esc(state.candidate.verification?.reason||'')}</p><details data-section="detail-candidate"><summary>查看候选长图</summary><a href="${safe(state.candidate.url)}" target="_blank" rel="noopener"><img ${window.designImage(state.candidate.url,960)} alt="待核实详情候选"></a></details>` : ''}
      ${state.preview.length && (run?.status !== 'completed' || state.candidate) && run?.status !== 'superseded' ? `<details open data-section="detail-preview"><summary>本次已生成 ${state.preview.length} ${whole?'张完整详情图':'段详情图'}</summary><div class="flow-delivery-grid">${state.preview.map(s => `<article><a href="${safe(s.url)}" target="_blank" rel="noopener"><img ${window.designImage(s.url)} alt="本次详情图"></a><p>${esc(s.verification?.reason||'已生成')}</p></article>`).join('')}</div></details>` : ''}
      ${!run?.parent_run_id && active ? action(id,'pause','暂停单段重做',false,`data-detail-run="${run.id}"`) : !run?.parent_run_id && run?.status === 'paused' && !run.quality_blocked ? action(id,'resume','继续单段重做',designBusy,`data-detail-run="${run.id}"`) : ''}
      ${run ? `<details data-section="detail-tasks"><summary>详情页详细记录（方案 / 出图）</summary>${window.renderDesignTaskLog(state.tasks.filter(t=>t.kind!=='master'),Boolean(state.design_contract))}</details>` : ''}</section>`;
  };
  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-detail-action]');
    if (!button) return;
    const id = Number(button.dataset.detailId), type = button.dataset.detailAction;
    if (pending.has(id)) return;
    window.designOperationStarted?.(id);
    pending.add(id); button.disabled = true;
    try {
      const base = `/api/design-projects/${id}/detail-page`;
      await api(['pause','resume'].includes(type) ? `${base}/${button.dataset.detailRun}/${type}` : base, {
        method:'POST', body:JSON.stringify({action:type,page_key:button.dataset.detailKey||'',note:document.querySelector('[data-design-note]')?.value||''})
      });
      const product = (items.design_projects || []).find(p=>p.id===id);
      if (product) product.flow = await api(`/api/design-projects/${id}/flow`);
      render();
    } catch (error) { notice(error.message,true); }
    finally { pending.delete(id); if(button.isConnected)button.disabled=false; window.refreshOpenDesign?.(id); }
  });
})();
