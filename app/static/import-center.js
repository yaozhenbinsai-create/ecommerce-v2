const designOrConfigRow = row;
row = function renderSupplierImport(batch) {
  if (page !== 'supplier_imports') return designOrConfigRow(batch);
  const confidence = batch.ai_confidence == null ? '尚未分析' : '初步识别完成';
  const processing=['AI排队中','AI解析中','等待重试'].includes(batch.status),retryInfo=batch.next_retry_at?` · 下次 ${batch.next_retry_at}`:'';
  const cells = [
    `<div class="item-name">${esc(batch.supplier_name)}</div><div class="sub">${esc(batch.filename)}</div>`,
    `<div>${batch.sheet_count} 个工作表 · ${batch.row_count} 行</div><div class="sub">提取嵌入图片 ${batch.image_count} 张 · 批次默认产地 ${esc([batch.default_origin_country,batch.default_origin_place].filter(Boolean).join('·')||'未设置（优先读取原表）')}</div>`,
    `<div>${esc(confidence)}</div><div class="sub">字段映射 ${(batch.field_mapping || []).length} 项</div>`,
    `<span class="badge ${batch.status.includes('失败') ? 'off' : ''}">${esc(batch.status)}</span><div class="sub">尝试 ${batch.analyze_attempts||0}/5${esc(retryInfo)}<br>${esc(batch.error_message || '原始文件和单元格来源已保留')}</div>`,
  ];
  return `<tr>${cells.map(cell => `<td>${cell}</td>`).join('')}<td><div class="actions import-actions">
    <button onclick="viewSupplierImport(${batch.id})">查看结构</button>
    ${batch.status==='已归档'?`<button onclick="archiveSupplierBatch(${batch.id})">查看归档</button>`:`<button onclick="analyzeSupplierImport(${batch.id})" ${batch.status === '解析失败'||processing ? 'disabled' : ''}>${processing?'后台处理中':'AI智能解析'}</button>
    <button onclick="pricingSupplierBatch(${batch.id})">批次定价</button>
    ${batch.status==='映射已确认'?`<button onclick="inspectSupplierBatch(${batch.id})">导入核对</button>`:''}
    <button onclick="confirmSupplierImport(${batch.id})" ${(batch.field_mapping || []).length ? '' : 'disabled'}>确认映射</button>
    <button class="delete" onclick="archiveSupplierBatch(${batch.id})">清理本批次</button>`}
  </div></td></tr>`;
};

let selectedImportFile = null;
let supplierUploadSession = null;

window.renderSupplierImportCards = function(batches) {
  if(!batches.length)return '<div class="empty"><b>从一份供应商货盘开始</b>上传 Excel，整理商品与图片，再核对实际规格。</div>';
  return `<div class="ws-import-intro"><span>01 <b>上传货盘</b></span><i>→</i><span>02 <b>整理字段与原图</b></span><i>→</i><span>03 <b>确认商品与规格</b></span><i>→</i><span>04 <b>进入商品库</b></span></div><div class="ws-import-list" data-list-cards="imports">${batches.map(batch=>{
    const archived=batch.status==='已归档',confirmed=batch.status==='映射已确认',processing=['AI排队中','AI解析中','等待重试'].includes(batch.status),mapped=(batch.field_mapping||[]).length;
    const primary=archived?`<button type="button" onclick="archiveSupplierBatch(${batch.id})">查看归档</button>`:confirmed?`<button type="button" class="primary" onclick="inspectSupplierBatch(${batch.id})">查看导入核对</button>`:mapped?`<button type="button" class="primary" onclick="confirmSupplierImport(${batch.id})">核对字段映射</button>`:`<button type="button" class="primary" onclick="analyzeSupplierImport(${batch.id})" ${batch.status==='解析失败'||processing?'disabled':''}>${processing?'后台处理中':'AI 智能解析'}</button>`;
    return `<article class="ws-import-card ${archived?'is-archived':''}" data-list-name="${esc(batch.supplier_name)}" data-list-state="${esc(batch.status)}" data-created-at="${esc(batch.created_at||'')}" data-updated-at="${esc(batch.updated_at||'')}"><div class="ws-import-file" aria-hidden="true"><span>X</span><small>XLSX</small></div><div class="ws-import-main"><div class="ws-card-topline"><span class="ws-label">供应商货盘</span><span class="badge ${batch.status.includes('失败')?'off':''}">${esc(batch.status)}</span></div><h3>${esc(batch.supplier_name||'未填写供应商')}</h3><p>${esc(batch.filename)}</p><div class="ws-import-metrics"><span><b>${batch.sheet_count||0}</b> 工作表</span><span><b>${batch.row_count||0}</b> 原表行</span><span><b>${batch.image_count||0}</b> 原图</span><span><b>${mapped}</b> 映射字段</span></div>${batch.error_message?`<p class="ws-warning">${esc(batch.error_message)}</p>`:`<span class="ws-muted">${archived?'历史批次可查看原始归档':confirmed?'商品与实际规格已归档，可继续核对来源资料':'原始文件、图片与单元格来源保留，确认前可核对字段'}</span>`}</div><div class="ws-import-actions">${primary}<button type="button" onclick="viewSupplierImport(${batch.id})">查看原表结构</button>${archived?'':`<details><summary>更多管理</summary><div><button type="button" onclick="pricingSupplierBatch(${batch.id})">批次定价</button>${mapped?`<button type="button" onclick="confirmSupplierImport(${batch.id})">确认映射</button>`:''}<button type="button" onclick="analyzeSupplierImport(${batch.id})" ${batch.status==='解析失败'||processing?'disabled':''}>${processing?'后台处理中':'重新解析'}</button><button type="button" class="delete" onclick="archiveSupplierBatch(${batch.id})">清理本批次</button></div></details>`}</div></article>`;
  }).join('')}</div>`;
};

async function inspectSupplierBatch(id) {
  try {
    const report=await api(`/api/supplier-imports/${id}/inspection`);
    ensureImportDialog();document.querySelector('#mappingTitle').textContent=`导入核对 · ${report.filename}`;
    const locations=x=>`${x.sheet||'原表'}${x.row?' 第 '+x.row+' 行':''}${x.column?' · '+x.column:''}`;
    document.querySelector('#mappingContent').innerHTML=`<section aria-label="导入检查结果">
      <h3>${report.status==='passed'?'导入一致性检查通过':'导入检查未通过'}</h3>
      <p>${report.products} 款商品 / 原表 ${report.expected_products} 款 · ${report.skus} 个规格 / 原表 ${report.expected_skus} 个 · ${report.images} 张原图</p>
      <p>已核对 ${report.matched_source_fields} 个原表字段；从明确备注识别 ${report.note_facts} 个参数，范围和单位完整保留。</p>
      ${report.errors.length?`<div class="c-error"><p>共 ${report.errors.length} 项不一致，以下先列前10项；可在下方按商品、字段或源表行号逐规格查看。</p>${report.errors.slice(0,10).map(x=>`<p>${esc(locations(x))}：${esc(x.message)}</p>`).join('')}</div>`:''}
      <p>下表逐规格列出备注参数和资料说明。近似值保留限定词，原表空白不猜填；导入一致不代表实物或平台验收通过。</p>
      <details><summary>原表留空的字段（不直接判定为导入错误）</summary><p>${Object.entries(report.source_blank_counts).map(([k,v])=>`${esc(k)} ${v} 行`).join('；')}</p></details>
      <label class="c-field">查找商品、规格或字段<input id="importInspectionSearch" placeholder="输入品名、规格、参数或源表行号"></label>
      <p role="status" id="importInspectionCount"></p><div id="importInspectionRows"></div><button type="button" id="importInspectionMore">再显示20个规格</button></section>`;
    let limit=20;
    const render=()=>{
      const query=document.querySelector('#importInspectionSearch').value.trim().toLowerCase();
      const found=report.rows.filter(x=>JSON.stringify(x).toLowerCase().includes(query));
      document.querySelector('#importInspectionCount').textContent=`显示 ${Math.min(limit,found.length)} 个，匹配 ${found.length} / 全部 ${report.skus} 个规格`;
      document.querySelector('#importInspectionMore').hidden=limit>=found.length;
      document.querySelector('#importInspectionRows').innerHTML=found.slice(0,limit).map(x=>`<details><summary>${esc(x.product_name)} · ${esc(x.name)} · 第 ${x.row} 行</summary>
        <p>${esc(x.sku)} · ${esc(locations(x))} · 售价 ${esc(x.price??'待补')} ${esc(x.currency||'')} · 关联原图 ${x.image_count} 张</p>
        ${report.errors.filter(e=>(e.row===x.row&&e.sheet===x.sheet)||e.sku===x.sku).map(e=>`<p class="c-error">${esc(e.column||'')}：${esc(e.message)}</p>`).join('')}
        ${x.note_facts.length?'<b>从本行明确备注识别</b>'+x.note_facts.map(f=>`<p>${esc(f.source.excerpt)} → ${esc(f.value)}<br><small>${esc(locations(f.source))}</small></p>`).join(''):''}
        ${x.additional_fields?.length?'<b>附加原文（用途未归类）</b>'+x.additional_fields.map(f=>`<p>${esc(f.label)}：${esc(f.value)}<br><small>${esc(f.message)}</small></p>`).join(''):''}
        ${x.notices.length?'<b>原资料说明及待核实项</b>'+x.notices.map(n=>`<p>${esc(n.label)}：${esc(n.message)}<br><small>${esc(locations(n.source||{}))}${n.evidence?.length?' · '+n.evidence.map(e=>esc(e.text)).join('；'):''}</small></p>`).join(''):'<p>未发现结构冲突或备注限定。</p>'}
        <button type="button" data-inspection-sku="${x.sku_id}">维护此规格</button></details>`).join('');
    };
    document.querySelector('#importInspectionSearch').addEventListener('input',()=>{limit=20;render();});
    document.querySelector('#importInspectionMore').addEventListener('click',()=>{limit+=20;render();});
    document.querySelector('#importInspectionRows').addEventListener('click',async event=>{
      const button=event.target.closest('[data-inspection-sku]');if(!button)return;
      document.querySelector('#mappingDialog').close();await window.commerceEditSku(Number(button.dataset.inspectionSku));
    });
    render();document.querySelector('#saveMapping').hidden=true;document.querySelector('#mappingDialog').showModal();
  }catch(error){notice(error.message,true)}
}

async function pricingSupplierBatch(id) {
  try {
    const current=await api(`/api/supplier-imports/${id}/pricing`),rule=current.rule;
    ensureImportDialog();document.querySelector('#mappingTitle').textContent=`批次 #${id} 定价`;
    document.querySelector('#mappingContent').innerHTML=`<p>售价 = 本规格真实供货价 × 倍率 + 每销售单位物流预留。仅应用到本批次，保留已有手工价格、零值和清空记录。缺少真实供货价或币种不一致时保留待处理。</p>
      <label class="c-field">供货倍率<input id="batchMultiplier" type="number" min="0.01" max="100" step="0.01" value="${esc(rule.multiplier)}"></label>
      <label class="c-field">物流预留金额<input id="batchFreight" type="number" min="0" step="0.01" value="${esc(rule.freight_reserve)}"></label>
      <label class="c-field">物流预留币种<input id="batchFreightCurrency" value="${esc(rule.freight_currency||'')}" placeholder="如 CNY"></label>
      <label class="c-field">定价依据<textarea id="batchPriceBasis" maxlength="1000">${esc(rule.authorization||'')}</textarea></label>
      <p class="c-hint">物流预留是商家估算，不代表承运商报价；不会执行平台更新。可在确认字段映射前先保存。</p>`;
    const save=document.querySelector('#saveMapping');save.hidden=false;save.disabled=false;save.textContent='保存本批次定价';
    save.onclick=async()=>{save.disabled=true;try{
      await api(`/api/supplier-imports/${id}/pricing`,{method:'PATCH',body:JSON.stringify({revision:current.revision,
        multiplier:document.querySelector('#batchMultiplier').value,freight_reserve:document.querySelector('#batchFreight').value,
        freight_currency:document.querySelector('#batchFreightCurrency').value,authorization:document.querySelector('#batchPriceBasis').value})});
      document.querySelector('#mappingDialog').close();await load();notice('本批次定价已保存，历史价格和手工值已保留');
    }catch(error){notice(error.message,true)}finally{save.disabled=false}};
    document.querySelector('#mappingDialog').showModal();
  }catch(error){notice(error.message,true)}
}

async function archiveSupplierBatch(id) {
  try {
    const current=await api(`/api/supplier-imports/${id}/archive`);
    ensureImportDialog();document.querySelector('#mappingTitle').textContent=`${current.archived?'查看归档':'清理本批次'} #${id}`;
    document.querySelector('#mappingContent').innerHTML=`<p>本批次共有 ${current.products} 款商品、${current.skus} 个规格。</p><p>清理会将本批次移出当前商品档案；原表、原图、旧字段和任务记录保留在归档中。新上传将建立新批次和新 SKU。</p>
      ${current.archived?`<details><summary>查看归档商品与规格</summary><pre>${esc(JSON.stringify(current.records,null,2))}</pre></details>`:
        `<p>${(current.blockers||[]).map(esc).join('；')||'当前处于导入阶段，可以归档后重新上传。'}</p>`}`;
    const save=document.querySelector('#saveMapping');save.hidden=false;save.disabled=!!current.blockers?.length;save.textContent=current.archived?'恢复此批次':'确认清理并保留归档';
    save.onclick=async()=>{save.disabled=true;try{
      await api(`/api/supplier-imports/${id}/${current.archived?'restore':'archive'}`,{method:'POST',body:JSON.stringify({token:current.token,reason:'导入检查后归档重导'})});
      document.querySelector('#mappingDialog').close();await load();notice(current.archived?'批次已恢复':'本批次已清理，可重新上传原文件；旧资料可在“查看归档”中追溯');
    }catch(error){notice(error.message,true)}finally{save.disabled=!!current.blockers?.length}};
    document.querySelector('#mappingDialog').showModal();
  }catch(error){notice(error.message,true)}
}

async function supplierUploadRequest(url, options) {
  for(let attempt=0;attempt<3;attempt++) {
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),30000);
    try {
      const response=await fetch(url,{credentials:'same-origin',headers:{'Content-Type':'application/json'},...options,signal:controller.signal});
      if(response.status===401){location.replace('/commerce-login');throw Error('登录已失效，请重新登录');}
      if(!response.ok){let details={};try{details=await response.json()}catch{}const error=Error(typeof details.detail==='string'?details.detail:'上传失败，请重试');error.status=response.status;throw error;}
      return await response.json();
    } catch(error) {
      if(attempt===2||(error.status&&error.status<500))throw error;
    } finally { clearTimeout(timer); }
  }
}
function supplierPhotoGuide() {
  return `<section class="import-field-guide" aria-label="产品实拍图要求">
    <b>产品实拍图要求：完整入镜，看清三个方向</b>
    <p>有厚度、弧度或复杂结构的产品，请提供同一规格的<strong>正视图、侧视图、俯视图</strong>。简单平面产品可按实际需要减少；三张相似斜拍图不能代替三个方向。</p>
    <p><strong>先统一摆放方向：</strong>以产品自然摆放、正面朝向拍摄者为准，X 为左右宽度，Y 为前后深度，Z 为上下高度。表头请写视图名称，不要只写“X图 / Y图 / Z图”。</p>
    <p><strong>正视图（X–Z，宽 × 高）：</strong>镜头从正前方平视，完整拍到左右边缘与上下边缘，看清正面轮廓、高度起伏和边缘弧度。</p>
    <p><strong>侧视图（Y–Z，深 × 高）：</strong>镜头从正侧方平视，看清前后轮廓、厚度、侧边弧度及部件连接；另一侧不同的，请补拍。</p>
    <p><strong>俯视图（X–Y，宽 × 深）：</strong>镜头在正上方垂直向下，完整拍到外轮廓，看清平面比例、四角弧度和图案分布。</p>
    <p><strong>每张都要满足：</strong>产品外轮廓完整入镜并留边，可见标签、配件不被截断或遮挡；光线均匀、边线和纹理清楚。镜头对准目标面，避免斜拍及近距离超广角。软质产品保持同一自然状态，不挤压、不拉伸；标签、纹样、接缝拍不清时另补近照。</p>
    <p><strong>Excel 放图：</strong>同一可销售规格一行，可分别设置“正视实拍图”“侧视实拍图”“俯视实拍图”“细节实拍图”列，将图片放在对应规格行。不同颜色、尺寸请分开；实际尺寸另填数值和单位，不能靠照片估算。图片提取成功不代表已满足设计要求，缺少的视角需补实拍，不能用 AI 生成图代替。</p>
  </section>`;
}

function ensureImportDialog() {
  if (document.querySelector('#importDialog')) return;
  document.body.insertAdjacentHTML('beforeend', `<dialog id="importDialog" class="import-dialog"><form method="dialog">
    <div class="dialog-head"><div><p class="eyebrow">SUPPLIER IMPORT</p><h2>导入供应商资料</h2></div><button class="icon-btn" value="cancel">×</button></div>
    <div class="embedded-image-note" style="margin:0 22px 14px"><b><a href="/static/templates/supplier-import-template-v1.xlsx?v=20261012" download="供应商商品资料模板_v1.xlsx">下载 Excel 填写模板</a></b><span>第一页含字段、虚构示例及正式填写区，第二页说明填写逻辑和拍摄要求。请从第 10 行填写真实商品；示例行不能导入。</span></div>
    <div class="import-form"><label>供应商名称 *<input id="importSupplier" placeholder="请输入供应商名称"></label><div><b class="upload-label">供应商 Excel *</b><input id="importFile" type="file" accept=".xlsx,.xlsm,.csv,.tsv" hidden><div id="excelDropzone" class="excel-dropzone" tabindex="0" role="button"><span class="upload-icon">⇧</span><strong>点击选择 Excel 文件</strong><span>或把文件拖到这里</span><small>支持 XLSX、XLSM、CSV、TSV，最大 30MB</small></div><div id="selectedFileInfo" class="selected-file-info" hidden></div></div><div class="embedded-image-note"><b>Excel 内嵌图片会自动读取</b><span>XLSX / XLSM 内嵌产品图不需要另外上传，系统会保留图片所在工作表和单元格位置。请在放图前核对以下拍摄要求。</span></div>
    ${supplierPhotoGuide()}
    <section class="import-field-guide"><b>供货表字段与填写示例</b><p><strong>一行代表一个可销售规格：</strong>按“颜色 × 尺寸”分别列行。例如双层蚕丝枕有白、灰、豆沙三色，就是三行；如果每色还有两种尺寸，就是六行。同款品名可重复，规格名称、颜色、尺寸和对应实物图分别填写。</p><p><strong>SKU 与商品条码：</strong>模板无需填写 SKU。文件导入后，系统为每个规格生成我们的 SKU，存入商品档案，亚马逊上架沿用该编号。GTIN（如 UPC/EAN）是商品条码；有则逐规格填写对应的真实号码，并核对商品与品牌。不同颜色或尺寸不要共用一个 GTIN，也不能拿 SKU 代填；没有 GTIN 就留空，上架前确认该品类的豁免结果。</p><p><strong>品牌与LOGO：</strong>供应商名称另列。不同产品请逐商品填写真实品牌；商品和包装都没有品牌名称或独特LOGO的，可在该商品品牌列写“无品牌”，系统会再对照实物图检查，或在商品档案选“确认无品牌”；只有LOGO而品牌归属不清的，可在品牌列写“仅LOGO”并在商品档案记录线索。批次默认品牌只适用于整批同一商品品牌，不能填供应商名称。</p><p><strong>商品与设计：</strong>品名、规格名称/颜色、面料成分与比例、填充物成分及用量、实际尺寸、商品总重、原产国和详细产地（例如“中国”“南通”）、对应规格的实物图。尺寸和重量请带单位。</p><p><strong>上架与履约：</strong>供货价及币种、包装尺寸/重量、可供数量或生产周期；已在 Amazon 上架的商品可另填 ASIN。供应商地址不等于产地；商品总重、填充净重和包装重量请分列。配送模板在店铺设置，不需要填进供应商 Excel。</p></section></div>
    <div class="dialog-foot"><button class="secondary" value="cancel">取消</button><button class="primary" type="button" id="uploadImport">上传并读取结构</button></div>
  </form></dialog><dialog id="mappingDialog" class="mapping-dialog"><form method="dialog">
    <div class="dialog-head"><div><p class="eyebrow">FIELD MAPPING</p><h2 id="mappingTitle">资料结构与字段确认</h2></div><button class="icon-btn" value="cancel">×</button></div>
    <div id="mappingContent" class="mapping-content"></div><div class="dialog-foot"><button class="secondary" value="cancel">关闭</button><button class="primary" type="button" id="saveMapping" hidden>确认字段映射</button></div>
  </form></dialog>`);
  document.querySelector('#uploadImport').onclick = uploadSupplierWorkbook;
  document.querySelector('#importSupplier').closest('label').insertAdjacentHTML('afterend','<label>批次默认品牌（可选）<input id="importBrand" maxlength="120" placeholder="表格没有品牌列时使用；不自动采用供应商名称"></label><label>原产国（表格没有时用批次默认）<input id="importOriginCountry" maxlength="120" placeholder="例如：中国"></label><label>详细产地（表格没有时用批次默认）<input id="importOriginPlace" maxlength="160" placeholder="例如：南通；不要填供应商地址"></label>');
  const input=document.querySelector('#importFile'),dropzone=document.querySelector('#excelDropzone');
  const choose=()=>input.click();
  dropzone.onclick=choose; dropzone.onkeydown=event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();choose()}};
  input.onchange=()=>setSelectedImportFile(input.files[0]||null);
  for(const eventName of ['dragenter','dragover'])dropzone.addEventListener(eventName,event=>{event.preventDefault();dropzone.classList.add('dragging')});
  for(const eventName of ['dragleave','drop'])dropzone.addEventListener(eventName,event=>{event.preventDefault();dropzone.classList.remove('dragging')});
  dropzone.addEventListener('drop',event=>setSelectedImportFile(event.dataTransfer.files[0]||null));
}

function setSelectedImportFile(file){selectedImportFile=file;const info=document.querySelector('#selectedFileInfo'),zone=document.querySelector('#excelDropzone');if(!file){info.hidden=true;zone.classList.remove('selected');return}const allowed=/\.(xlsx|xlsm|csv|tsv)$/i.test(file.name);if(!allowed){selectedImportFile=null;notice('请选择 XLSX、XLSM、CSV 或 TSV 文件。',true);return}zone.classList.add('selected');info.hidden=false;info.innerHTML=`<span>✓</span><div><b>${esc(file.name)}</b><small>${(file.size/1024/1024).toFixed(2)} MB · 已选择</small></div><button type="button" id="clearImportFile">重新选择</button>`;document.querySelector('#clearImportFile').onclick=()=>{selectedImportFile=null;document.querySelector('#importFile').value='';info.hidden=true;zone.classList.remove('selected')}}

function openImportDialog() {
  ensureImportDialog();
  document.querySelector('#importSupplier').value = '';
  document.querySelector('#importBrand').value = '';
  document.querySelector('#importOriginCountry').value = '';
  document.querySelector('#importOriginPlace').value = '';
  document.querySelector('#importFile').value = '';
  selectedImportFile=null;document.querySelector('#selectedFileInfo').hidden=true;document.querySelector('#excelDropzone').classList.remove('selected');
  document.querySelector('#importDialog').showModal();
}

async function uploadSupplierWorkbook() {
  const supplier = document.querySelector('#importSupplier').value.trim();
  const file = selectedImportFile || document.querySelector('#importFile').files[0];
  if (!supplier || !file) { notice('请填写供应商名称并选择 Excel 文件。', true); return; }
  if (file.size > 30 * 1024 * 1024) { notice('文件超过 30MB，请拆分后上传。', true); return; }
  const button = document.querySelector('#uploadImport');
  button.disabled = true; button.textContent = '正在读取工作簿…';
  try {
    const dataUrl = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(file); });
    const content=String(dataUrl).split(',')[1],bytes=Uint8Array.from(atob(content),char=>char.charCodeAt(0));
    const digest=await crypto.subtle.digest('SHA-256',bytes),sha256=Array.from(new Uint8Array(digest),byte=>byte.toString(16).padStart(2,'0')).join('');
    const metadata={supplier_name:supplier,default_brand:document.querySelector('#importBrand').value.trim(),default_origin_country:document.querySelector('#importOriginCountry').value.trim(),default_origin_place:document.querySelector('#importOriginPlace').value.trim(),filename:file.name,size:file.size,sha256};
    const signature=JSON.stringify(metadata);
    if(supplierUploadSession?.signature!==signature)supplierUploadSession={signature,id:crypto.randomUUID().replaceAll('-','')};
    const session=await supplierUploadRequest('/api/supplier-imports/upload-session',{method:'POST',body:JSON.stringify({...metadata,upload_id:supplierUploadSession.id})});
    const characters=session.chunk_bytes*4/3,total=Math.ceil(content.length/characters);
    for(let index=0;index<total;index++) {
      button.textContent=`正在上传工作簿 ${Math.round(index/total*100)}%…`;
      await supplierUploadRequest(`/api/supplier-imports/upload-session/${session.upload_id}/${index}`,{method:'PUT',body:JSON.stringify({content_base64:content.slice(index*characters,(index+1)*characters)})});
    }
    button.textContent='文件已传完，正在读取结构…';
    await supplierUploadRequest(`/api/supplier-imports/upload-session/${session.upload_id}/finish`,{method:'POST',body:'{}'});
    supplierUploadSession=null;
    document.querySelector('#importDialog').close(); notice('供应商文件已保存并完成结构读取'); await load();
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; button.textContent = '上传并读取结构'; }
}

function batchById(id) { return (items.supplier_imports || []).find(item => item.id === id); }

function workbookPreview(batch) {
  const sheets = batch.workbook_snapshot?.sheets || [];
  return sheets.map(sheet => {
    const rows = (sheet.rows || []).slice(0, 12);
    const values = rows.map(row => Array.isArray(row) ? row : row.values || []);
    const width = Math.min(Math.max(0, ...values.map(row => row.length)), 12);
    return `<section class="sheet-preview"><h3>${esc(sheet.name)} <small>${sheet.max_row} 行 × ${sheet.max_column} 列</small></h3><div class="preview-scroll"><table><tbody>${values.map((row, index) => `<tr><th>${rows[index]?.row || index + 1}</th>${row.slice(0,width).map(value => `<td>${esc(value)}</td>`).join('')}</tr>`).join('')}</tbody></table></div></section>`;
  }).join('') || '<div class="model-check-empty">没有可预览的工作表内容</div>';
}

function extractedImageGallery(batch) {
  const images = batch.extracted_images || [];
  const guide = `<details><summary>核对产品实拍图要求</summary>${supplierPhotoGuide()}</details>`;
  if (!images.length) return '<div class="model-check-empty">当前文件没有提取到嵌入图片；如使用图片链接，请另行核对对应实拍图。</div>' + guide;
  return `<section class="extracted-images"><div class="image-extract-summary"><b>已提取嵌入图片 ${images.length} 张</b><span>图片保留原始工作表与锚点位置。提取成功仅表示文件已读取；请逐规格核对产品是否完整入镜、视角是否足够、细节是否清楚。</span></div>${guide}<div class="extracted-image-gallery">${images.map((item, index) => `<figure><a href="${esc(item.url)}" target="_blank" title="打开原图"><img ${window.designImage(item.url,320)} alt="提取图片 ${index + 1}"></a><figcaption><b>图片 ${index + 1}</b><span>${esc(item.sheet || '工作表')} · ${esc(item.cell || `第 ${item.row || '?'} 行`)}</span></figcaption></figure>`).join('')}</div></section>`;
}

function viewSupplierImport(id) {
  ensureImportDialog(); const batch = batchById(id); if (!batch) return;
  document.querySelector('#mappingTitle').textContent = `${batch.supplier_name} · ${batch.filename}`;
  document.querySelector('#mappingContent').innerHTML = `<label class="c-field"><span>批次默认品牌</span><input id="batchBrand" maxlength="120" value="${esc(batch.default_brand||'')}" placeholder="表格没有品牌列时使用"></label><label class="c-field"><span>批次默认原产国</span><input id="batchOriginCountry" maxlength="120" value="${esc(batch.default_origin_country||'')}" placeholder="例如：中国"></label><label class="c-field"><span>批次默认详细产地</span><input id="batchOriginPlace" maxlength="160" value="${esc(batch.default_origin_place||'')}" placeholder="例如：南通"></label><p class="c-hint">原表中对应商品或 SKU 的明确值优先；批次默认值保留独立来源。Amazon 原产国只取国家，城市保留在商品资料中。</p>`+workbookPreview(batch) + extractedImageGallery(batch);
  const save=document.querySelector('#saveMapping');save.hidden=batch.status==='已归档';save.disabled=false;save.textContent='保存批次默认信息';save.onclick=async()=>{save.disabled=true;try{await api(`/api/supplier-imports/${id}/defaults`,{method:'PATCH',body:JSON.stringify({default_brand:document.querySelector('#batchBrand').value.trim(),default_origin_country:document.querySelector('#batchOriginCountry').value.trim(),default_origin_place:document.querySelector('#batchOriginPlace').value.trim()})});await load();document.querySelector('#mappingDialog').close();notice('批次品牌和产地已保存；已有商品的空缺产地已按来源补充')}catch(e){notice(e.message,true)}finally{save.disabled=false}};
  document.querySelector('#mappingDialog').showModal();
}

async function analyzeSupplierImport(id) {
  notice('AI解析任务正在进入后台队列…');
  try { await api(`/api/supplier-imports/${id}/analyze`, {method:'POST',body:'{}'}); notice('任务已进入后台，断线或超时会自动冷却重试'); await load(); }
  catch (error) { notice(error.message, true); }
}

const standardFields=['supplier_sku','product_name','variant_name','category','brand','cost_price','sale_price','cost_currency','sale_currency','price_unit','color','material','fabric','fill_material','fill_weight','size','weight','item_length','item_width','item_height','natural_height','compressed_height','components','description','image','image_url','package_dimensions','package_weight','package_quantity','number_of_items','gtin_upc','asin','origin','origin_country','origin_place','manufacturer','model_name','model_number','item_shape','pattern','special_feature'];
async function confirmSupplierImport(id) {
  let batch;
  try { batch=await api(`/api/supplier-imports/${id}/mapping-preview`); }
  catch(error){notice(error.message,true);return;}
  ensureImportDialog(); if(!batch)return;
  const mappings=batch.field_mapping||[];
  document.querySelector('#mappingTitle').textContent=`确认字段映射 · ${batch.filename}`;
  const parsePlan=batch.workbook_snapshot?.parse_plan;
  const mappingWarning=(batch.unmapped_columns||[]).length?`<p class="c-hint">${batch.unmapped_columns.map(x=>`${esc(x.sheet)} · ${esc(x.column)}（${x.rows.length}行）：${x.field?'表头存在歧义，建档前必须明确对应字段':'将保留为附加原文，用途未归类，不自动作为设计或平台参数'}`).join('；')}</p>`:'';
  document.querySelector('#mappingContent').innerHTML=`<div class="mapping-note">确认后建立商品档案和实际规格；缺项保留为待补充。低置信度或冲突字段需核对后才能用于设计。${parsePlan?'':'解析计划缺失，请重新 AI 解析或明确填写各工作表的商品区域后再确认。'}</div><details ${parsePlan?'':'open'}><summary>工作表、表头与商品数据区域（必填）</summary><p>每个商品区域须明确 sheet_name、header_rows、start_row、end_row；非商品工作表须列入 excluded_sheets 并说明原因。</p><textarea id="importParsePlan" rows="10">${esc(JSON.stringify(parsePlan||{regions:[],excluded_sheets:[]},null,2))}</textarea></details><div class="mapping-list">${mappings.map((mapping,index)=>`<div class="mapping-row" data-map-row data-map-index="${index}"><select data-map-standard>${standardFields.map(field=>`<option value="${field}" ${field===mapping.standard_field?'selected':''}>${field}</option>`).join('')}</select><input data-map-source value="${esc(mapping.source_column)}" placeholder="供应商列名"><input data-map-confidence type="number" min="0" max="1" step="0.01" value="${Number(mapping.confidence??1)}"><input data-map-reason value="${esc(mapping.reason||'')}" placeholder="识别依据"><button type="button" onclick="this.closest('[data-map-row]').remove()">×</button></div>`).join('')}</div>`;
  document.querySelector('#mappingContent').insertAdjacentHTML('afterbegin',mappingWarning);
  const save=document.querySelector('#saveMapping'); save.hidden=false;save.disabled=false;save.textContent='确认字段映射'; save.onclick=()=>saveSupplierMapping(id,batch);
  document.querySelector('#mappingDialog').showModal();
}

async function saveSupplierMapping(id,previewBatch=null) {
  const button=document.querySelector('#saveMapping');if(button.disabled)return;
  button.disabled=true;button.textContent='正在建立商品档案…';
  let progress=document.querySelector('#mappingSubmitStatus');
  if(!progress){progress=document.createElement('p');progress.id='mappingSubmitStatus';progress.setAttribute('role','status');document.querySelector('#mappingContent').prepend(progress);}
  progress.textContent='正在核对映射并提交建档，请稍候。';
  try {
    const batch=previewBatch||batchById(id);if(!batch)throw Error('导入批次已更新，请关闭后重新读取映射');
    const mappings=[...document.querySelectorAll('#mappingDialog [data-map-row]')].map(row=>{
      const previous=batch.field_mapping[Number(row.dataset.mapIndex)];
      if(!previous)throw Error('字段映射已更新，请关闭后重新读取映射');
      const source=row.querySelector('[data-map-source]').value.trim();
      return {...previous,standard_field:row.querySelector('[data-map-standard]').value,source_column:source,
        column_index:source===previous.source_column?previous.column_index:undefined,
        column_number:source===previous.source_column?previous.column_number:undefined,
        confidence:Number(row.querySelector('[data-map-confidence]').value||0),reason:row.querySelector('[data-map-reason]').value.trim()};
    }).filter(item=>item.source_column);
    const planInput=document.querySelector('#importParsePlan');if(!planInput)throw Error('请明确填写商品解析区域后再确认');
    const payload={mappings,parse_plan:JSON.parse(planInput.value)};
    const result=await api(`/api/supplier-imports/${id}/confirm`,{method:'POST',body:JSON.stringify(payload)});
    document.querySelector('#mappingDialog').close();
    notice(`已建立 ${result.products_created||0} 个商品档案，关联 ${result.products_updated||0} 个已有商品，新增 ${result.skus_created||0} 个规格；已自动安排 ${result.classification_queued||0} 个店铺站点的亚马逊类目同步`);await load();await inspectSupplierBatch(id);
  } catch(error){progress.textContent=error.message;notice(error.message,true);}
  finally{button.disabled=false;button.textContent='确认字段映射';}
}

const renderBeforeImports = render;
render = function renderImportCenter() {
  renderBeforeImports();
  if (page === 'supplier_imports') {
    $('#panelHint').textContent='每个可销售规格一行，填写产地、材料、尺寸、重量等资料；实拍产品须完整入镜，有厚度或弧度的产品请提供正视、侧视、俯视图。拍摄要求与字段清单见“新增导入”。';
    const q=$('#search').value.trim().toLowerCase(),batches=(items.supplier_imports||[]).filter(x=>JSON.stringify(x).toLowerCase().includes(q));
    const archived=batches.filter(x=>x.status==='已归档').length;
    $('#stats').innerHTML=`<div class="stat"><small>全部批次</small><b>${batches.length}</b></div><div class="stat"><small>当前批次</small><b>${batches.length-archived}</b></div><div class="stat"><small>历史归档</small><b>${archived}</b></div>`;
  }
};

const regularAddAction = $('#add').onclick;
$('#add').onclick = () => page === 'supplier_imports' ? openImportDialog() : regularAddAction();
window.viewSupplierImport=viewSupplierImport; window.analyzeSupplierImport=analyzeSupplierImport; window.confirmSupplierImport=confirmSupplierImport;

let importRefreshBusy=false;
setInterval(async()=>{if(page!=='supplier_imports'||importRefreshBusy)return;const active=(items.supplier_imports||[]).some(batch=>['AI排队中','AI解析中','等待重试'].includes(batch.status));if(!active)return;importRefreshBusy=true;try{await load();const completed=(items.supplier_imports||[]).find(batch=>batch.status==='待人工确认'&&(batch.field_mapping||[]).length);if(completed)notice(`“${completed.filename}”AI解析完成，请确认字段映射`)}finally{importRefreshBusy=false}},8000);
