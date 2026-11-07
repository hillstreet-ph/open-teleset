import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';
const html=readFileSync('static/dashboard.html','utf8');
function method(start,end,api={}) {
  return vm.runInNewContext(`({${html.slice(html.indexOf(start),html.indexOf(end))}})`,{_api:api,crypto:{randomUUID:()=> 'synthetic'}});
}
const helpers=method('loginConnectionPayload() {','// QR Login Methods');
test('default, direct and selected produce unambiguous connection payloads',()=>{
  for(const mode of ['default','direct','selected']) {
    const payload=helpers.loginConnectionPayload.call({loginProxyMode:mode,loginProxyId:'one'});
    assert.equal(payload.proxy_mode,mode);
    assert.equal(payload.proxy_id,mode==='selected'?'one':undefined);
  }
  assert.throws(()=>helpers.loginConnectionPayload.call({loginProxyMode:'selected',loginProxyId:''}),/Choose/);
});
test('phone accepts full international number without duplicating country code and suppresses repeated clicks',async()=>{
  let calls=0,payload,options,release;
  const api={post:async(path,body,config)=>{calls++;payload=body;options=config;await new Promise(r=>release=r);return {data:{success:true}};}};
  const methods=method('async sendPhoneCode() {','async verifyPhoneCode()',api);
  const state={...methods,loginConnectionPayload:helpers.loginConnectionPayload,loginProxyMode:'direct',phoneAccountId:'synthetic',phoneData:{countryCode:'91',phone:'+91 55555 00000'},authRevision:1,showQrModal:true,addLog(){}};
  const pending=state.sendPhoneCode();await state.sendPhoneCode();
  assert.equal(calls,1);assert.equal(payload.phone,'+915555500000');assert.equal(payload.proxy_mode,'direct');assert.equal(options.timeout,65000);
  release();await pending;assert.equal(state.phoneStep,'code');assert.equal(state.phoneLoading,false);
});
test('hex import clears credential after successful verification',async()=>{
  let calls=0;
  const methods=method('loginConnectionPayload() {','// QR Login Methods',{post:async(path,body,config)=>{
    calls++;assert.equal(path,'/api/accounts/add-hex-session');assert.equal(body.dc_id,5);assert.equal(body.proxy_mode,'selected');assert.equal(body.proxy_id,'one');assert.ok(config.timeout>22000);return {data:{success:true}};
  }});
  const state={...methods,hexData:{key:'synthetic',dc:5},hexAccountId:'synthetic',loginProxyMode:'selected',loginProxyId:'one',authRevision:1,showQrModal:true,showToast(){},loadAccounts(){}};
  await state.importHexSession();assert.equal(calls,1);assert.equal(state.hexData.key,'');assert.equal(state.showQrModal,false);
});
test('overlapping QR refresh clicks share one cancellation and retain generated account ID',async()=>{
  let release,cancels=0,generated=[];
  const methods=method('async refreshQr() {','async submit2FA()', {delete:async()=>{cancels++;await new Promise(r=>release=r);}});
  const state={...methods,qrAccountId:'old',stopQrPolling(){},async generateQr(){generated.push(this.qrAccountId);}};
  const pending=state.refreshQr();await state.refreshQr();
  assert.equal(cancels,1);release();await pending;
  assert.equal(generated.length,1);assert.equal(generated[0],state.qrAccountId);assert.equal(state.qrRefreshing,false);
});
test('app credentials are optional and sent only as a complete valid pair',()=>{
  const hash='0123456789abcdef0123456789abcdef';
  const base={loginProxyMode:'default'};
  assert.deepEqual({...helpers.loginConnectionPayload.call({...base})},{proxy_mode:'default'});
  assert.deepEqual({...helpers.loginConnectionPayload.call({...base,apiCredData:{id:'',hash:''}})},{proxy_mode:'default'});
  const sent=helpers.loginConnectionPayload.call({...base,apiCredData:{id:' 4242 ',hash:hash.toUpperCase()}});
  assert.equal(sent.api_id,'4242');assert.equal(sent.api_hash,hash.toUpperCase());
  assert.throws(()=>helpers.loginConnectionPayload.call({...base,apiCredData:{id:'4242',hash:''}}),/both/);
  assert.throws(()=>helpers.loginConnectionPayload.call({...base,apiCredData:{id:'abc',hash}}),/whole number/);
  assert.throws(()=>helpers.loginConnectionPayload.call({...base,apiCredData:{id:'4242',hash:'not-hex'}}),/32 hexadecimal/);
});
test('hex import forwards the app credentials with the key',async()=>{
  let payload;
  const methods=method('loginConnectionPayload() {','// QR Login Methods',{post:async(path,body)=>{payload=body;return {data:{success:true}};}});
  const state={...methods,hexData:{key:'synthetic',dc:3},hexAccountId:'synthetic',loginProxyMode:'direct',
    apiCredData:{id:'4242',hash:'0'.repeat(32)},authRevision:1,showQrModal:true,showToast(){},loadAccounts(){}};
  await state.importHexSession();
  assert.equal(payload.api_id,'4242');assert.equal(payload.api_hash,'0'.repeat(32));
});
test('export dialog adds the hex backup with its data center and app ID',async()=>{
  const calls=[];
  const methods=method('async exportSession(id) {','copyAccountId(accountId)', {get:async(url)=>{
    calls.push(url);
    if(url.endsWith('/export-hex-session')) return {data:{hex_key:'0011ff',dc_id:4,api_id:4242}};
    return {data:{session_string:'string-session'}};
  }});
  const state={...methods,exportData:{id:'',session:''},showExportModal:false,addLog(){},showToast(){}};
  await state.exportSession('acc');
  assert.deepEqual(calls,['/api/accounts/acc/export-session','/api/accounts/acc/export-hex-session']);
  assert.equal(state.exportData.session,'string-session');
  assert.equal(state.exportData.hexKey,'0011ff');
  assert.equal(state.exportData.hexDc,4);
  assert.equal(state.exportData.apiId,4242);
  assert.equal(state.showExportModal,true);
});
test('missing hex backup leaves the string export usable',async()=>{
  const methods=method('async exportSession(id) {','copyAccountId(accountId)', {get:async(url)=>{
    if(url.endsWith('/export-hex-session')) {const e=new Error('nope');e.response={data:{detail:'No key'}};throw e;}
    return {data:{session_string:'string-session'}};
  }});
  const state={...methods,exportData:{id:'',session:''},showExportModal:false,addLog(){},showToast(){}};
  await state.exportSession('acc');
  assert.equal(state.exportData.session,'string-session');
  assert.equal(state.exportData.hexKey,'');
  assert.equal(state.exportData.hexError,'No key');
});
