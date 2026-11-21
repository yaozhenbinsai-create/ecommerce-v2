/* Product facts precede design; Seller Central evidence remains merchant-provided. */
(() => {
  const clone=value=>value===undefined?undefined:JSON.parse(JSON.stringify(value));
  const metadata=new Set(['marketplace_id','language_tag']);
  function resolveRule(rule,root,seen=[]) {
    let result={...(rule||{})};
    if(result.$ref?.startsWith('#/')&&!seen.includes(result.$ref)) {
      let value=root;
      for(const segment of result.$ref.slice(2).split('/'))value=value?.[segment.replace(/~1/g,'/').replace(/~0/g,'~')];
      if(value)result={...resolveRule(value,root,[...seen,result.$ref]),...result};
      delete result.$ref;
    }
    for(const part of result.allOf||[]) {
      const resolved=resolveRule(part,root,seen);
      result={...resolved,...result,properties:{...(resolved.properties||{}),...(result.properties||{})},
        required:[...new Set([...(resolved.required||[]),...(result.required||[])])]};
    }
    const choices=[...(result.anyOf||[]),...(result.oneOf||[])].map(p=>resolveRule(p,root,seen));
    const enumerated=choices.find(p=>p.enum);
    if(!result.enum&&enumerated)result={...enumerated,...result,enum:enumerated.enum,enumNames:enumerated.enumNames};
    if(!result.type)result.type=choices.find(p=>p.type)?.type||(result.properties?'object':result.items?'array':undefined);
    if(!result.properties)result.properties=choices.find(p=>p.properties)?.properties;
    if(!result.items)result.items=choices.find(p=>p.items)?.items;
    return result;
  }
  function create(deps) {
    const {E,input,textarea,select,table,button,show,call,api,dialog,notice,getData,reload,openPage}=deps;
    const data=()=>getData()||{},find=(kind,id)=>(data()[kind]||[]).find(x=>x.id===Number(id));
    const states={needs_application:'需在 Seller Central 办理',submitted:'商家登记申请中',
      merchant_provided_approval:'商家已提供平台批准回执',rejected:'商家登记平台拒绝',revoked:'批准依据已撤回'};
    const money=(v,c)=>v===null||v===undefined||v===''?'待补充':`${E(v)} ${E(c||'币种待确认')}`;
    const price=(row,key,currency,source)=>`${money(row[key],row[currency])}<small>${E(row[source]?.message||({source:'原表明确值',fallback:'业务预设',manual:'商家维护'})[row[source]?.status]||'来源待核对')}</small>`;
    const missing=(entries=[])=>entries.map(x=>E(typeof x==='string'?x:(x.label||x.field||'资料')+'：'+(x.reason||'待补充'))).join('<br>');
    const targetName=t=>`${t.account_name||'未命名账号'} · ${t.name||t.marketplace_id} · ${{production:'正式',sandbox:'沙盒',mock:'模拟'}[t.environment]||t.environment}`;
    const targets=()=>data().target||[];
    let sequence=0,nodeSequence=0,bulkSession=null,view='all';
    function active(form,version){return version===sequence&&dialog.open&&dialog.querySelector('form')===form;}
    function loading(title){const version=++sequence;show(title,'<p>正在读取商品档案…</p>');return {version,form:dialog.querySelector('form')};}
    function render(rows) {
      const all=typeof rows==='function'?rows('product'):rows||data().product||[];
      const needsFacts=p=>(p.design_readiness?.skus||[]).some(s=>!s.ready)||!p.design_readiness?.skus?.length;
      const products=all.filter(p=>view==='all'||(view==='design'?Boolean(p.project_id):needsFacts(p)));
      const tabs=[['all','全部商品',all.length],['design','已进入设计',all.filter(p=>p.project_id).length],['facts','资料待补充',all.filter(needsFacts).length]];
      const toolbar=`<div class="ws-library-toolbar"><div class="ws-segments" aria-label="商品视图">${tabs.map(([key,title,count])=>`<button type="button" data-workspace-product-view="${key}" aria-pressed="${view===key}">${title}<span>${count}</span></button>`).join('')}</div><details class="ws-bulk"><summary>批量操作</summary><div>${button('archive-design-all',0,'整批设计并上架')}${button('archive-resume-all',0,'继续整批流程')}${button('archive-reprice',0,'按定价规则重算')}</div></details></div>`;
      if(!products.length)return toolbar+'<div class="empty"><b>当前视图没有匹配的商品</b>可切换“全部商品”或调整搜索；新货盘从右上角导入。</div>';
      return toolbar+`<div class="ws-product-grid" data-list-cards="products">${products.map(p=>{
          const skus=(data().sku||[]).filter(s=>s.product_id===p.id&&s.active!==false);
          const report=p.design_readiness||{};
          const readyCount=skus.filter(s=>report.skus?.some(r=>r.sku_id===s.id&&r.ready)).length;
          const image=(p.source_images||[])[0]||skus.flatMap(s=>s.source_images||[])[0];
          const thumbnail=image&&window.designImage?`<img ${window.designImage(image,640)} alt="${E(p.name)}供应商原图">`:'<span class="ws-product-placeholder" aria-hidden="true">◇</span>';
          const currencies=[...new Set(skus.filter(s=>s.price!==null&&s.price!==undefined&&s.price!=='').map(s=>s.currency))];
          const values=skus.map(s=>Number(s.price)).filter((n,i)=>skus[i].price!==null&&skus[i].price!==undefined&&skus[i].price!==''&&Number.isFinite(n));
          const range=currencies.length===1&&values.length===skus.length&&values.length?`${Math.min(...values)===Math.max(...values)?E(Math.min(...values)):E(Math.min(...values))+'–'+E(Math.max(...values))} ${E(currencies[0]||'币种待确认')}`:'按规格查看';
          const state=skus.length&&readyCount===skus.length?'资料可设计':'资料待补充';
          const skuContent=skus.map(s=>{const ready=(report.skus||[]).find(r=>r.sku_id===s.id);
              const sourceCells=(s.source_image_references||[]).map(r=>r.reference_cell).filter(Boolean);
              return `<article class="ws-sku-row"><div><strong>${E(s.name||s.sku)}</strong><small>${E((p.variant_structure?.rows||[]).find(row=>row.sku_id===s.id)?.details?.map(row=>row.label+'：'+row.value).join(' · ')||((s.color||'原表未写颜色')+' · '+(s.size||'尺寸待补充')))}</small><small class="ws-mono">${E(s.sku)}</small>${s.design_reference?'<small>当前设计输入：AI 修正参考 · 供应商原图保留</small>':''}${sourceCells.length?`<small>${s.design_reference?'保留的款式原图':'款式共享原图'}：${E(sourceCells.join('、'))}</small>`:''}<small class="${ready?.ready?'ws-good':'ws-warning'}">${ready?.ready?'基础设计字段齐全':missing(ready?.missing||[])||'设计资料待核对'}</small>${ready?.source_notices?.length?`<details><summary>原资料说明 / 待核实 ${ready.source_notices.length} 项</summary>${ready.source_notices.map(n=>`<small>${E(n.label)}：${E(n.message)}</small>`).join('')}</details>`:''}</div><dl><div><dt>采购价</dt><dd>${price(s,'cost','cost_currency','cost_source')}</dd></div><div><dt>销售价</dt><dd>${price(s,'price','currency','price_source')}</dd></div></dl>${button('sku-edit',s.id,'维护此 SKU')}</article>`;
            }).join('')||'<p>实际 SKU 待补充</p>';
          return `<article class="ws-product-card" data-created-at="${E(p.created_at||'')}" data-updated-at="${E(p.updated_at||'')}" data-list-name="${E(p.name)}" data-list-state="${E(state)}"><div class="ws-product-image">${thumbnail}<span class="ws-product-count">${E(p.variant_structure?.label||skus.length+' 个规格')}</span></div><div class="ws-product-body"><div class="ws-card-topline"><span class="ws-label ${readyCount===skus.length&&skus.length?'ws-good':''}">${state}</span>${p.project_id?'<span class="ws-label ws-ai-label">已有设计项目</span>':''}</div><h3>${E(p.name)}</h3>${p.variant_structure?.distinct_products?`<p class="ws-warning">${E(p.variant_structure.note)}</p>`:''}<p class="ws-product-brand">品牌：${E(p.brand||'待确认')}<span>商品 #${E(p.id)}</span></p><div class="ws-product-price"><span>当前销售价</span><strong>${range}</strong></div><div class="ws-product-ready"><span>${readyCount} / ${skus.length} 个规格具备基础设计资料</span><progress max="${skus.length||1}" value="${readyCount}" aria-label="基础设计资料准备进度"></progress></div><div class="ws-product-primary">${button('product-edit',p.id,'维护商品档案','class="primary"')}${button('archive-design',p.id,'选择 SKU 设计')}</div><details class="ws-sku-details"><summary>查看 ${skus.length} 个规格与价格</summary><div class="ws-sku-list">${skuContent}</div></details><details class="ws-product-tools"><summary>平台字段与更多管理</summary><div class="ws-product-classification">${(p.amazon_classifications||[]).map(x=>`<p><b>${E(x.target_name)}</b> · ${E(x.product_type||'类目查询中')}<small>${E(['queued','running','retry_wait'].includes(x.job?.status)?'正在自动同步亚马逊类目…':x.category_path||x.job?.error||x.status?.message||'等待自动同步')}</small></p>`).join('')}</div><div class="actions">${button('archive-fields',p.id,'Amazon 商品字段')}${button('archive-model-names',p.id,'批量填写型号名称')}${button('archive-classification',p.id,'同步亚马逊类目')}${button('archive-evidence',p.id,'GTIN 豁免依据')}</div></details></div></article>`;
        }).join('')}</div><p class="ws-library-note">销售价格使用该批次保存的定价规则，已有手工价格保留。展开规格可查看采购价、销售价和各自来源。</p>`;
    }
    async function design(id) {
      const guard=loading('选择实际 SKU 开始设计'),report=await api(`/api/commerce/products/${id}/readiness`);
      if(!active(guard.form,guard.version))return;
      const skus=(data().sku||[]).filter(s=>s.product_id===Number(id)&&s.active!==false);
      show('选择实际 SKU 开始设计',`<p>只选择有对应原图且设计事实齐全的 SKU。平台豁免、授权和配送可以稍后办理。</p>${(report.skus||[]).map(r=>{const sku=skus.find(s=>s.id===r.sku_id)||{};
        return `<label class="target-choice"><input type="checkbox" name="sku_ids" value="${E(r.sku_id)}" ${r.ready?'checked':'disabled'}><span>${E(sku.name||sku.sku||'SKU #'+r.sku_id)}<small>${r.ready?'可设计':missing(r.missing)}</small></span></label>`;
      }).join('')}${report.missing?.some(x=>!x.sku_id)?`<p class="c-error">${missing(report.missing.filter(x=>!x.sku_id))}</p>`:''}`,async f=>{
        const ids=f.getAll('sku_ids').map(Number);if(!ids.length)throw Error('请选择至少一个资料齐全的 SKU');
        const result=await call(`/products/${id}/design`,{sku_ids:ids});
        return {afterSave:()=>{openPage('design_projects');notice(`已准备设计项目 #${result.project_id}，可开始设计所选 SKU`);}};
      },'打开设计项目');
    }
    async function designAll() {
      const work=[],blocked=[];
      const destinations=targets().filter(target=>target.environment==='production'&&target.enabled!==false);
      for(const product of data().product||[]) {
        if(destinations.length&&destinations.every(target=>(data().listing||[]).some(row=>row.product_id===product.id&&row.target_id===target.id&&row.status==='buyable')))continue;
        const available=(data().sku||[]).filter(s=>s.product_id===product.id&&s.active!==false);
        const eligible=(product.design_readiness?.skus||[]).filter(r=>r.ready&&available.some(s=>s.id===r.sku_id));
        if(eligible.length)work.push({id:product.id,name:product.name,sku_ids:eligible.map(r=>r.sku_id)});
        const missingRows=(product.design_readiness?.skus||[]).filter(r=>!r.ready&&available.some(s=>s.id===r.sku_id));
        if(missingRows.length)blocked.push({name:product.name,count:missingRows.length,missing:missingRows.flatMap(r=>r.missing||[])});
      }
      if(!work.length)throw Error('暂无资料齐全且需要处理的商品，请补齐商品档案中的具体缺项');
      const signature=JSON.stringify(work.map(row=>[row.id,row.sku_ids]));
      if(bulkSession?.signature!==signature)bulkSession={signature,id:window.crypto.randomUUID(),started:new Set()};
      const session=bulkSession;
      show('整批设计并上架',`<p>本次 ${work.length} 个商品 · ${work.reduce((n,row)=>n+row.sku_ids.length,0)} 个资料齐全的规格。按已配置并启用的全部正式店铺、站点自动生成设计、商品资料，校验后上架并回读平台结果。</p><div data-bulk-design-progress role="status">尚未启动</div>${blocked.length?`<details><summary>${blocked.reduce((n,row)=>n+row.count,0)} 个规格待补充</summary>${blocked.map(row=>`<p><strong>${E(row.name)}</strong> · ${row.count} 个规格<br>${missing(row.missing)}</p>`).join('')}</details>`:''}`,async()=>{
        const failures=[],progress=dialog.querySelector('[data-bulk-design-progress]');
        for(const row of work) {
          if(session.started.has(row.id))continue;
          if(progress)progress.textContent=`已启动 ${session.started.size}/${work.length} 个商品；正在核对 ${row.name} 的设计资料…`;
          try {
            const prepared=await call(`/products/${row.id}/design`,{sku_ids:row.sku_ids});
            await call(`/design-projects/${prepared.project_id}/listing-workflow-batches`,{request_id:session.id+':'+row.id});
            session.started.add(row.id);
          } catch(error) {failures.push(`${row.name}：${error.message}`);}
        }
        if(failures.length)throw Error(`已启动 ${session.started.size} 个商品；${failures.join('；')}`);
        return {afterSave:()=>{openPage('design_projects');notice(`已启动 ${session.started.size} 个商品的设计和自动上架流程，具体平台结果见各商品进度`);}};
      },'开始设计并上架');
    }
    function typeFor(product,target) {
      const category=(data().category||[]).find(c=>c.id===product.category_id||(c.business_ids||[]).includes(product.business_id));
      return (data().category_mapping||[]).find(m=>m.enabled!==false&&m.category_id_local===category?.id&&m.marketplace_id===target?.marketplace_id)?.product_type||product.product_type||'';
    }
    function control(rule,value,root,scope,title,dirty,depth=0) {
      const resolved=resolveRule(rule,root),id='archive-node-'+(++nodeSequence),kind=resolved.type;
      const selector=`[data-archive-node="${id}"]`,old=clone(value);
      const label=title||resolved.title||'字段';
      let read,bind=()=>{},html;
      if(depth>10||resolved.$ref||(!kind&&!resolved.enum)||(kind==='object'&&!Object.keys(resolved.properties||{}).length)) {
        html=`<label class="c-field c-wide"><span>${E(label)}（JSON）</span><textarea data-archive-node="${id}">${E(value===undefined?'':JSON.stringify(value,null,2))}</textarea></label>`;
        read=host=>{const v=host.querySelector(selector)?.value.trim();return v?JSON.parse(v):undefined;};
      } else if(kind==='array') {
        let entries=(Array.isArray(value)&&value.length?value:[undefined]).map(v=>control(resolved.items||{},v,root,scope,resolved.items?.title||'内容',dirty,depth+1));
        const inner=()=>entries.map((entry,i)=>`<div class="archive-array-item">${entry.html}<button type="button" data-archive-remove="${i}">移除此项</button></div>`).join('')+'<button type="button" data-archive-add>添加一项</button>';
        html=`<fieldset data-archive-node="${id}"><legend>${E(label)}</legend>${inner()}</fieldset>`;
        read=host=>{const box=host.querySelector(selector);return entries.map(e=>e.read(box)).filter(v=>v!==undefined);};
        bind=host=>{const box=host.querySelector(selector);if(!box)return;
          const attach=()=>{entries.forEach(e=>e.bind(box));box.querySelector('[data-archive-add]').onclick=()=>{
            const current=entries.map(e=>e.read(box));entries=current.map(v=>control(resolved.items||{},v,root,scope,'内容',dirty,depth+1));
            entries.push(control(resolved.items||{},undefined,root,scope,'内容',dirty,depth+1));box.innerHTML=inner();attach();dirty();
          };box.querySelectorAll('[data-archive-remove]').forEach(b=>b.onclick=()=>{
            const current=entries.map(e=>e.read(box));current.splice(Number(b.dataset.archiveRemove),1);
            entries=current.map(v=>control(resolved.items||{},v,root,scope,'内容',dirty,depth+1));box.innerHTML=inner();attach();dirty();
          });};attach();};
      } else if(kind==='object') {
        const children=Object.entries(resolved.properties||{}).map(([name,child])=>{
          const fixed=metadata.has(name)?scope[name]:value?.[name];
          return {name,node:control(child,fixed,root,scope,child.title||name,dirty,depth+1)};
        });
        html=`<fieldset data-archive-node="${id}"><legend>${E(label)}</legend><div class="c-grid">${children.map(x=>metadata.has(x.name)?`<div hidden>${x.node.html}</div>`:x.node.html).join('')}</div></fieldset>`;
        read=host=>{const box=host.querySelector(selector),result=old&&typeof old==='object'&&!Array.isArray(old)?clone(old):{};let useful=false;
          for(const {name,node} of children){const v=node.read(box);if(v===undefined||(Array.isArray(v)&&!v.length))delete result[name];else{result[name]=v;if(!metadata.has(name))useful=true;}}
          return useful||Object.keys(result).some(k=>!metadata.has(k)&&!children.some(x=>x.name===k))?result:undefined;
        };
        bind=host=>{const box=host.querySelector(selector);children.forEach(x=>x.node.bind(box));};
      } else {
        const choices=resolved.enum||(kind==='boolean'?[true,false]:null);
        if(choices)html=`<label class="c-field"><span>${E(label)}</span><select data-archive-node="${id}"><option value="">未填写</option>${choices.map((v,i)=>`<option value="${E(JSON.stringify(v))}" ${JSON.stringify(v)===JSON.stringify(value)?'selected':''}>${E(resolved.enumNames?.[i]||String(v))}</option>`).join('')}</select></label>`;
        else if(resolved.const!==undefined)html=`<input type="hidden" data-archive-node="${id}" value="${E(String(resolved.const))}">`;
        else html=`<label class="c-field"><span>${E(label)}</span><input data-archive-node="${id}" type="${['number','integer'].includes(kind)?'number':'text'}" ${kind==='integer'?'step="1"':kind==='number'?'step="any"':''} value="${E(value??'')}"></label>`;
        read=host=>{const v=host.querySelector(selector)?.value;if(v===undefined||v==='')return undefined;
          if(choices)return JSON.parse(v);if(resolved.const!==undefined)return resolved.const;
          if(['number','integer'].includes(kind)){const n=Number(v);if(!Number.isFinite(n)||(kind==='integer'&&!Number.isInteger(n)))throw Error(label+'需要有效数字');return n;}return v;
        };
      }
      return {html,read,bind};
    }
    async function modelNames(id,targetId) {
      const product=find('product',id);if(!product)throw Error('商品档案不存在');
      const available=targets().filter(t=>t.environment==='production'&&t.enabled!==false);
      const target=available.find(t=>t.id===Number(targetId))||available[0];
      if(!target)throw Error('请先配置并启用正式店铺与站点');
      const guard=loading('批量填写型号名称 · '+product.name);
      const view=await api(`/api/commerce/products/${id}/platform-fields?target_id=${target.id}`);
      if(!active(guard.form,guard.version))return;
      if(!view.ready||!view.schema?.properties?.model_name)throw Error('当前店铺官方规则没有可用的型号名称字段，请先同步类目与官方字段');
      const skus=view.skus||[];
      show('批量填写型号名称 · '+product.name,
        `${select('model_target','店铺与站点',target.id,available.map(t=>[t.id,targetName(t)]))}<p>填写该站点语言的真实产品名称。仅更新所选规格的型号名称；厂家型号编号、其他商品事实、价格、库存、图片及历史记录保留。</p>${input('model_name','此商品的型号名称','')}<div>${skus.map(s=>`<label class="target-choice"><input type="checkbox" name="model_sku_ids" value="${E(s.sku_id)}" checked><span>${E(s.name)}<small>当前型号名称：${E((s.attributes?.model_name||[]).map(v=>v.value).filter(Boolean).join(' / ')||'未填写')}</small></span></label>`).join('')}</div><p data-model-name-progress role="status"></p>`,async f=>{
          const value=String(f.get('model_name')||'').trim();if(!value)throw Error('请填写真实产品名称');
          const selected=new Set(f.getAll('model_sku_ids').map(Number));if(!selected.size)throw Error('请选择实际规格');
          let count=0;const progress=dialog.querySelector('[data-model-name-progress]');
          for(const sku of skus.filter(s=>selected.has(s.sku_id))){
            const old=sku.attributes?.model_name||[];
            const attributes={model_name:[{value,marketplace_id:target.marketplace_id,language_tag:old[0]?.language_tag||target.language||'en_US'}]};
            if(JSON.stringify(old)!==JSON.stringify(attributes.model_name)||(sku.cleared_fields||[]).includes('model_name')){
              try{await call(`/products/${id}/platform-fields?target_id=${target.id}`,{sku_id:sku.sku_id,revision:sku.revision,schema_hash:view.schema_hash,attributes,cleared_fields:(sku.cleared_fields||[]).filter(x=>x!=='model_name')},'PUT');}
              catch(error){throw Error(`已保存 ${count}/${selected.size} 个规格；${sku.name}：${error.message}。请重新打开，已保存记录和历史保留。`);}
            }
            count++;if(progress)progress.textContent=`已保存 ${count}/${selected.size} 个规格`;
          }
          return `已核对 ${count} 个规格的型号名称；重新组装上架资料后须再次通过完整平台校验`;
        },'保存所选规格的型号名称');
      dialog.querySelector('[name=model_target]').onchange=e=>modelNames(id,Number(e.currentTarget.value)).catch(error=>notice(error.message,true));
    }
    async function fields(id,targetId,skuId) {
      const product=find('product',id);if(!product)throw Error('商品档案不存在');
      const available=targets();if(!available.length){show('Amazon 商品字段','<p>请先配置店铺与站点，再读取对应的 Amazon 商品字段。</p>');return;}
      const target=available.find(t=>t.id===Number(targetId))||available[0],guard=loading('Amazon 商品字段 · '+product.name);
      const view=await api(`/api/commerce/products/${id}/platform-fields?target_id=${target.id}`);
      if(!active(guard.form,guard.version))return;
      const sku=(view.skus||[]).find(s=>s.sku_id===Number(skuId))||view.skus?.[0];
      const top=`<div class="c-grid">${select('archive_target','店铺与站点',target.id,available.map(t=>[t.id,targetName(t)]))}${select('archive_sku','实际 SKU',sku?.sku_id,(view.skus||[]).map(s=>[s.sku_id,s.name]))}</div><p>${E(view.message)}</p><p>Amazon 商品类型：${E(view.product_type||'待确定')}</p>`;
      if(!view.ready||!sku) {
        show('Amazon 商品字段 · '+product.name,top+`<div class="actions"><button type="button" data-archive-sync ${view.product_type?'':'disabled'}>读取当前店铺官方字段</button><button type="button" data-archive-refresh>刷新字段</button></div>`);
        bindPickers();dialog.querySelector('[data-archive-sync]').onclick=async e=>{e.currentTarget.disabled=true;try{await call(`/targets/${target.id}/sync`,{scope:'definition',product_type:view.product_type});notice('已安排只读获取官方字段，任务完成后点击刷新字段');}catch(error){notice(error.message,true);e.currentTarget.disabled=false;}};
        dialog.querySelector('[data-archive-refresh]').onclick=()=>fields(id,target.id,sku?.sku_id).catch(e=>notice(e.message,true));return;
      }
      let base=clone(sku.attributes||{}),cleared=new Set(sku.cleared_fields||[]),dirtyFields=new Set(),clearTouched=new Set(),controllers=new Map();
      const scope={marketplace_id:target.marketplace_id,language_tag:target.language||'en_US'};
      const formHtml=()=>{controllers=new Map();return (view.fields||[]).map(field=>{
        const changed=()=>dirtyFields.add(field.name),node=control(field.rule,base[field.name],view.schema,scope,field.title,changed);controllers.set(field.name,node);
        const origin=sku.attribute_sources?.[field.name],originLabel={fabric:'面料',material:'材料'};
        const fallback=origin?.status==='fallback'?`<p class="c-hint">缺失补齐：来自${E(origin.scope==='product'?'商品共用资料':'本规格资料')}的${E(originLabel[origin.source_field]||origin.source_field)}：${E(origin.source_value)}。有本字段真实值时优先使用真实值。</p>`:'';
        return `<section data-archive-attribute="${E(field.name)}"><h3>${E(field.title)}${field.required?' · 必填':''}</h3>${fallback}${field.description?`<p class="c-hint">${E(field.description)}</p>`:''}${node.html}<label class="c-hint"><input type="checkbox" data-archive-clear="${E(field.name)}" ${cleared.has(field.name)?'checked':''}>明确清空此字段，不重新使用原表或预设值</label></section>`;
      }).join('')||'<p>暂无可编辑的商品字段。</p>';};
      const body=top+`<p class="c-hint">先保存已有真实资料，缺项仍会显示。切换店铺或 SKU 前请先保存当前修改；文案、图片与报价在刊登阶段维护。</p>${sku.issues?.length?`<details open><summary>此 SKU 待补充字段</summary><p class="c-error">${missing(sku.issues)}</p></details>`:''}${select('archive_mode','编辑方式','form',[['form','字段表单'],['json','高级 JSON']])}<div data-archive-fields>${formHtml()}</div><div data-archive-json hidden>${textarea('archive_attributes','当前 SKU 的完整商品属性 JSON',JSON.stringify(base,null,2))}</div>`;
      show('Amazon 商品字段 · '+product.name,body,async()=>{
        const values=readValues();
        await call(`/products/${id}/platform-fields?target_id=${target.id}`,{sku_id:sku.sku_id,revision:sku.revision,schema_hash:view.schema_hash,attributes:values,cleared_fields:[...cleared]},'PUT');
        return {afterSave:()=>fields(id,target.id,sku.sku_id)};
      },'保存此 SKU 资料');
      bindPickers();bindFields();
      function readForm(){const result=clone(base);for(const name of dirtyFields){const node=controllers.get(name),v=node?.read(dialog);if(v===undefined||(Array.isArray(v)&&!v.length)){delete result[name];cleared.add(name);}else{result[name]=v;cleared.delete(name);}}
        dialog.querySelectorAll('[data-archive-clear]').forEach(el=>{if(el.checked){cleared.add(el.dataset.archiveClear);delete result[el.dataset.archiveClear];}else if(clearTouched.has(el.dataset.archiveClear))cleared.delete(el.dataset.archiveClear);});return result;}
      function readValues(){return dialog.querySelector('[name=archive_mode]').value==='json'?readJson():readForm();}
      function readJson(){const value=JSON.parse(dialog.querySelector('[name=archive_attributes]').value||'{}');if(!value||typeof value!=='object'||Array.isArray(value))throw Error('商品属性需要 JSON 对象');for(const name of Object.keys(base))if(!(name in value))cleared.add(name);for(const name of Object.keys(value))cleared.delete(name);return value;}
      function bindFields(){controllers.forEach(node=>node.bind(dialog));dialog.querySelectorAll('[data-archive-attribute]').forEach(section=>{
        section.oninput=section.onchange=event=>{dirtyFields.add(section.dataset.archiveAttribute);if(event.target?.dataset.archiveClear)clearTouched.add(event.target.dataset.archiveClear);};
      });}
      dialog.querySelector('[name=archive_mode]').onchange=e=>{
        try{if(e.currentTarget.value==='json'){base=readForm();dirtyFields.clear();dialog.querySelector('[name=archive_attributes]').value=JSON.stringify(base,null,2);}
          else{base=readJson();dirtyFields.clear();dialog.querySelector('[data-archive-fields]').innerHTML=formHtml();bindFields();}
          dialog.querySelector('[data-archive-fields]').hidden=e.currentTarget.value==='json';dialog.querySelector('[data-archive-json]').hidden=e.currentTarget.value!=='json';
        }catch(error){e.currentTarget.value='json';notice(error.message,true);}
      };
      function bindPickers(){dialog.querySelector('[name=archive_target]').onchange=e=>fields(id,e.currentTarget.value).catch(error=>notice(error.message,true));dialog.querySelector('[name=archive_sku]').onchange=e=>fields(id,target.id,e.currentTarget.value).catch(error=>notice(error.message,true));}
    }
    async function evidence(id,targetId,selection={}) {
      const product=id==null?null:find('product',id);if(id!=null&&!product)throw Error('商品档案不存在');
      const available=targets().filter(t=>t.environment==='production');
      if(!available.length){show('GTIN 豁免依据','<p>请先配置真实店铺与站点。豁免申请和批准结果在 Seller Central 办理。</p>');return;}
      const target=available.find(t=>t.id===Number(targetId))||available[0],title='GTIN 豁免回执 · '+(product?.name||targetName(target)),guard=loading(title);
      const ptype=product?typeFor(product,target):(selection.product_type||''),brand=product?(product.brand||''):(selection.brand||'');
      const view=brand&&ptype?await api(`/api/commerce/targets/${target.id}/gtin-evidence?brand=${encodeURIComponent(brand)}&product_type=${encodeURIComponent(ptype)}`):{matching_evidence:[]};
      if(!active(guard.form,guard.version))return;
      const old=view.evidence||{};
      const body=`<p>无商品编码的商品会在上架过程中自动检验，通过后继续发布。此处可留存 Seller Central 的实际批准回执，同一店铺、站点、品牌和商品类型的商品共用。</p><p>系统不授予豁免，也不通过 API 查询批准状态。</p><a href="https://sellercentral.amazon.com/" target="_blank" rel="noopener noreferrer">前往 Seller Central</a><div class="c-grid">${select('evidence_target','真实店铺与站点',target.id,available.map(t=>[t.id,targetName(t)]))}${input('brand','实际品牌',brand,'text',product?'readonly':'')}${input('product_type','Amazon 商品类型',ptype,'text',product?'readonly':'')}${product?'':'<button type="button" data-evidence-scope-load>读取本范围记录</button>'}${select('status','Seller Central 显示的结果（商家登记）',old.status||'needs_application',Object.entries(states))}${input('reference','批准结果页面或回执引用（可无编号）',old.reference||'')}${input('approval_date','平台批准日期',old.approval_date||'','date')}${input('approved_category','平台实际批准的类目',old.approved_category||'')}${textarea('notes','办理说明',old.notes||'')}</div><details><summary>批准回执附件（可选）</summary>${textarea('attachments','附件引用 JSON（上传路径或 HTTPS 网址）',JSON.stringify(old.attachments||[],null,2))}</details><p class="c-hint">接口技术支持工单、空限制列表、刊登预检成功均不能作为商品批准回执。</p>${view.latest_listing_validation?`<p>最近商品检验：${E(({validated:'通过',rejected:'未通过',pending:'待完成'})[view.latest_listing_validation.status]||'待完成')} · 仅对应当时的商品资料。</p>`:''}<details open><summary>已留存回执与历史状态</summary>${(view.matching_evidence||[]).map(r=>`<p>${E(states[r.status]||r.status)} · ${E(r.approval_date||r.registered_at||'')}<br>${E(r.reference||'')} · ${E(r.approved_category||'')}<br>登记人：${E(r.registered_by||'未记录')} · 修订 ${E(r.revision)}</p>`).join('')||'<p>尚未留存回执；后续刊登仍会自动检验。</p>'}</details>`;
      show(title,body,async f=>{
        const attachments=JSON.parse(f.get('attachments')||'[]');if(!Array.isArray(attachments))throw Error('回执附件需要 JSON 数组');
        const nextBrand=(f.get('brand')||'').trim(),nextType=(f.get('product_type')||'').trim();
        if(old.id&&(nextBrand!==brand||nextType!==ptype))throw Error('范围已改变，请先读取本范围记录后再保存');
        const payload={brand:nextBrand,product_type:nextType,source:'seller_central',status:f.get('status'),reference:f.get('reference'),approval_date:f.get('approval_date'),approved_category:f.get('approved_category'),attachments,notes:f.get('notes')};
        if(old.id){payload.revision=old.revision;await call(`/gtin-evidence/${old.id}`,payload,'PATCH');}
        else await call(`/targets/${target.id}/gtin-evidence`,payload);
        return {afterSave:()=>evidence(id,target.id,{brand:nextBrand,product_type:nextType})};
      },'保存批准回执');
      const selectedScope=()=>({brand:dialog.querySelector('[name=brand]').value,product_type:dialog.querySelector('[name=product_type]').value});
      dialog.querySelector('[name=evidence_target]').onchange=e=>evidence(id,e.currentTarget.value,selectedScope()).catch(error=>notice(error.message,true));
      if(!product)dialog.querySelector('[data-evidence-scope-load]').onclick=()=>evidence(null,target.id,selectedScope()).catch(error=>notice(error.message,true));
    }
    async function handle(action,id) {
      if(action==='archive-resume-all') {
        const ids=(data().product||[]).filter(row=>row.project_id&&row.active!==false).map(row=>row.id);
        const result=await call('/products/resume-workflows',{product_ids:ids});
        await reload();openPage('design_projects');
        notice(`已继续 ${result.resumed} 个店铺流程${result.failures?.length?`；${result.failures.map(row=>row.reason).join('；')}`:''}`);return true;
      }
      if(action==='archive-reprice') {
        const ids=(data().product||[]).filter(row=>row.supplier_source&&row.active!==false).map(row=>row.id);
        const result=await call('/products/reprice',{product_ids:ids});
        await reload();openPage('commerce_products');
        notice(`已核对 ${result.skus} 个规格，更新 ${result.changed} 个售价；原报价和手工价格已保留`);return true;
      }
      if(action==='archive-design-all'){await designAll();return true;}
      if(action==='archive-design'){await design(Number(id));return true;}
      if(action==='archive-classification'){const result=await call(`/products/${Number(id)}/sync-classification`,{});notice(`已安排 ${result.jobs.length} 个店铺站点的类目同步，完成后自动保存并补齐未提交资料`);await reload();return true;}
      if(action==='archive-model-names'){await modelNames(Number(id));return true;}
      if(action==='archive-fields'){await fields(Number(id));return true;}
      if(action==='archive-evidence'){await evidence(Number(id));return true;}
      return false;
    }
    window.commerceOpenDesignFacts=async pid=>{
      const product=await call(`/products/from-design/${Number(pid)}`,{});
      if(!product?.id)throw Error('设计项目未关联有效商品档案，请核对商品记录');
      if(typeof window.commerceEditProductArchive!=='function')throw Error('商品档案编辑器尚未就绪，请刷新页面');
      await window.commerceEditProductArchive(product.id);
    };
    return {render,handle,evidence,setView:value=>{view=['all','design','facts'].includes(value)?value:'all';}};
  }
  window.CommerceProductArchive={create,resolveRule};
})();
