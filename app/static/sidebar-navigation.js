/* Enhance the server-rendered navigation without replacing its first paint. */
(() => {
  const nav=document.querySelector('#nav');
  if(!nav)return;
  const storageKey='shop-ops.navigation.groups.v1';
  let saved=null;
  try{const raw=localStorage.getItem(storageKey),value=raw===null?null:JSON.parse(raw);if(Array.isArray(value))saved=value}catch{}
  const buttons=[...nav.querySelectorAll('[data-page]')],sections=[...nav.querySelectorAll('[data-nav-group]')];
  for(const section of sections){
    section.open=(saved?saved.includes(section.dataset.navGroup):section.dataset.navGroup!=='settings')||Boolean(section.querySelector('.nav-item.active'));
    section.addEventListener('toggle',()=>{try{localStorage.setItem(storageKey,JSON.stringify(sections.filter(s=>s.open).map(s=>s.dataset.navGroup)))}catch{}});
  }
  const sidebar=nav.closest('.sidebar');
  const mobileToggle=sidebar.querySelector('.nav-menu-toggle');
  function setMobileOpen(open){sidebar.classList.toggle('nav-mobile-open',open);mobileToggle.setAttribute('aria-expanded',String(open))}
  mobileToggle.onclick=()=>setMobileOpen(mobileToggle.getAttribute('aria-expanded')!=='true');
  let activePage='';
  function syncSelection(){
    const active=nav.querySelector('.nav-item.active');
    for(const button of buttons){if(button===active)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current')}
    for(const section of sections)section.classList.toggle('has-active',section.contains(active));
    if(active){
      mobileToggle.textContent='功能菜单 · '+(active.querySelector('.nav-label')?.textContent||active.textContent);
      if(active.dataset.page!==activePage){const section=active.closest('.nav-group');if(section)section.open=true;activePage=active.dataset.page}
    }
  }
  new MutationObserver(syncSelection).observe(nav,{subtree:true,attributes:true,attributeFilter:['class']});
  nav.addEventListener('click',event=>{if(event.target.closest('[data-page]'))setMobileOpen(false)});
  sidebar.addEventListener('keydown',event=>{if(event.key==='Escape'&&sidebar.classList.contains('nav-mobile-open')){setMobileOpen(false);mobileToggle.focus()}});
  syncSelection();
})();
