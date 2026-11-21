// Every category uses the same flexible specification structure. Amazon field
// names are stored beside factory-facing Chinese values for later listing APIs.
for (const [priceKey,currencyKey,title] of [['cost_price','cost_currency','成本'],['sale_price','sale_currency','保底售价']]) {
  const index=pages.businesses.fields.findIndex(f=>f[0]===priceKey);
  pages.businesses.fields[index][1]=title+'金额';
  pages.businesses.fields.splice(index+1,0,[currencyKey,title+'币种','selectValue',[['','待确认'],['CNY','人民币 CNY'],['USD','美元 USD'],['EUR','欧元 EUR'],['GBP','英镑 GBP'],['JPY','日元 JPY'],['CAD','加元 CAD']]]);
}
const currencyBusinessRow=row;
row=function(x){const html=currencyBusinessRow(x);if(page!=='businesses')return html;
  return html.replace(/(成本|Cost) ¥[^<·]+/,(_,label)=>`${label} ${esc(x.cost_price??'—')} ${esc(x.cost_currency||'币种待确认')} `)
    .replace(/(销售|Sale) ¥[^<]+/,()=>`保底售价 ${esc(x.sale_price??'—')} ${esc(x.sale_currency||'币种待确认')}`);
};
const businessFieldHtml = fieldHtml;
fieldHtml = function renderBusinessField(field, item) {
  const [key, label, type] = field;
  if (type === 'hiddenlist') {
    return `<textarea hidden name="${key}">${esc(JSON.stringify(item?.[key] || []))}</textarea>`;
  }
  if (type !== 'specmanager') return businessFieldHtml(field, item);
  return `<div class="field full spec-field" data-field="${key}">
    <label>${label}</label>
    <div class="spec-help">参数可以来自任何品类；“亚马逊属性”保存官方字段名，“变体”表示不同 SKU 可选择不同值。</div>
    <div class="spec-table" data-spec-table></div>
    <button type="button" class="secondary spec-add" data-spec-add>＋ 添加参数</button>
    <textarea hidden name="${key}" data-spec-json>${esc(JSON.stringify(item?.[key] || []))}</textarea>
  </div>`;
};

function specRow(spec = {}) {
  return `<div class="spec-row">
    <input data-spec="group" value="${esc(spec.group || '通用参数')}" placeholder="参数组">
    <input data-spec="name" value="${esc(spec.name || '')}" placeholder="参数名称">
    <input data-spec="value" value="${esc(spec.value || '')}" placeholder="参数值/可选值">
    <input data-spec="unit" value="${esc(spec.unit || '')}" placeholder="单位">
    <input data-spec="amazon_attribute" value="${esc(spec.amazon_attribute || '')}" placeholder="亚马逊属性">
    <label class="spec-check"><input type="checkbox" data-spec="required" ${spec.required ? 'checked' : ''}>必填</label>
    <label class="spec-check"><input type="checkbox" data-spec="variant" ${spec.variant ? 'checked' : ''}>变体</label>
    <button type="button" class="spec-remove" title="删除参数">×</button>
  </div>`;
}

function setupSpecManager() {
  const field = document.querySelector('.spec-field');
  if (!field || field.dataset.ready) return;
  field.dataset.ready = '1';
  const table = field.querySelector('[data-spec-table]');
  const store = field.querySelector('[data-spec-json]');
  let specs = [];
  try { specs = JSON.parse(store.value || '[]'); } catch (_) {}
  table.innerHTML = specs.map(specRow).join('') || '<div class="model-check-empty">尚未配置参数，可从类目模板或手动添加</div>';
  const sync = () => {
    const rows = [...table.querySelectorAll('.spec-row')];
    store.value = JSON.stringify(rows.map(row => {
      const value = name => row.querySelector(`[data-spec="${name}"]`);
      return {
        group: value('group').value.trim(), name: value('name').value.trim(),
        value: value('value').value.trim(), unit: value('unit').value.trim(),
        amazon_attribute: value('amazon_attribute').value.trim(),
        required: value('required').checked, variant: value('variant').checked,
      };
    }).filter(spec => spec.name));
  };
  field.querySelector('[data-spec-add]').onclick = () => {
    table.querySelector('.model-check-empty')?.remove();
    table.insertAdjacentHTML('beforeend', specRow());
    sync();
  };
  table.addEventListener('input', sync);
  table.addEventListener('change', sync);
  table.addEventListener('click', event => {
    const button = event.target.closest('.spec-remove');
    if (button) { button.closest('.spec-row').remove(); sync(); }
  });
}

new MutationObserver(() => queueMicrotask(setupSpecManager)).observe(document.querySelector('#fields'), {childList: true});
