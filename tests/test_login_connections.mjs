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
