/* Display derivatives only; source links, AI inputs and listing media stay original. */
(() => {
  const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const sizes=[320,640,960,1280];
  function attributes(url,width=640,options={}){
    const commerce=/^\/api\/commerce\/media\/file\/([a-f0-9]{64}\.jpg)$/.exec(url||'');
    const local=/^\/(uploads|design-outputs)\/[^?#]+$/.test(url||'');
    const original=commerce||local||options.remote&&/^https:\/\//.test(url||'')?url:'';
    const size=sizes.includes(width)?width:640;
    const path=commerce?'/commerce-media/'+commerce[1]:local?original:'';
    const display=path?'/image-previews'+path+'?width='+size+(size<=640?'&height='+size:''):original;
    return `src="${escape(display)}" data-original="${escape(original)}" loading="lazy" decoding="async"${path?' title="点击查看原图"':''}`;
  }
  window.ShopImages={attributes};
  window.designImage=attributes;
  document.addEventListener('error',event=>{
    const img=event.target,original=img?.dataset?.original;
    if(img?.tagName==='IMG'&&original&&!img.dataset.previewFailed&&img.getAttribute('src')?.startsWith('/image-previews/')){
      img.dataset.previewFailed='1';img.src=original;
    }
  },true);
  document.addEventListener('click',event=>{
    const img=event.target;
    if(!event.defaultPrevented&&img?.tagName==='IMG'&&img.dataset?.original&&
       img.getAttribute('title')==='点击查看原图'&&!img.closest('a,button')){
      window.open(img.dataset.original,'_blank','noopener');
    }
  });
})();
