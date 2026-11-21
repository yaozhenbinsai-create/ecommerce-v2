const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
function setup(overrides={}) {
  const context={window:{}};vm.runInNewContext(fs.readFileSync('app/static/platform-authorization.js','utf8'),context);
  const report={account_id:9,account_name:'Shop',platform:'亚马逊',seller_id:'SELLER',mode:'file',revision:'v1',supported:true,credential_env:'',credentials:{fields:{client_id:true,client_secret:true,refresh_token:true},error:''},targets:[],...overrides};
  const state={mode:{value:'file'},calls:[],refreshes:0};
  const boxes=Object.fromEntries(['[data-auth-file]','[data-auth-env]'].map(k=>[k,{hidden:false,querySelectorAll:()=>[]}]));
  const ui=context.window.createPlatformAuthorization({E:String,input:(key,label,value)=>`<input name="${key}" value="${value||''}">${label}`,select:(key,label,value)=>{state.mode.value=value;return label},table:(heads,rows)=>JSON.stringify([heads,rows]),show:(title,html,save)=>Object.assign(state,{title,html,save}),call:async(path,payload)=>{state.calls.push({path,payload});return {message:'saved'}},api:async()=>report,dialog:{querySelector:s=>s==='[name=mode]'?state.mode:boxes[s]},refresh:async()=>state.refreshes++});
  return {ui,state,boxes};
}
test('account without regions can configure credentials without receiving saved values',async()=>{
  const {ui,state}=setup();await ui.open(9);
  for(const key of ['seller_id','client_id','client_secret','refresh_token'])assert.match(state.html,new RegExp(`name="${key}"`));
  assert.match(state.html,/尚未配置真实店铺地区/);
  await state.save({get:key=>({seller_id:'SELLER',mode:'file'})[key]});
  assert.equal(state.calls[0].path,'/accounts/9/credentials');assert.equal(state.calls[0].payload.client_secret,'');assert.equal(state.calls[0].payload.revision,'v1');assert.equal(state.refreshes,1);
});
test('missing environment offers page fields and advanced mode can still be selected',async()=>{
  const {ui,state,boxes}=setup({mode:'environment',credential_env:'AUTH_ENV',credentials:{fields:{},error:'missing'}});await ui.open(9);
  assert.equal(state.mode.value,'file');assert.equal(boxes['[data-auth-file]'].hidden,false);
  state.mode.value='environment';state.mode.onchange();assert.equal(boxes['[data-auth-file]'].hidden,true);assert.equal(boxes['[data-auth-env]'].hidden,false);
});
test('unimplemented platforms never present an Amazon credential submission',async()=>{
  const {ui,state}=setup({supported:false,platform:'Other'});await ui.open(9);assert.match(state.html,/尚未接入/);assert.equal(state.save,undefined);
});
