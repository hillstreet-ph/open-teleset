import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';
function handler(name, env={}, fetcher=()=>{throw Error('unexpected network')}) {
 let handle;
 const code=stripTypeScriptTypes(readFileSync(`supabase/functions/${name}/index.ts`,'utf8').replace(/^import .*;\n/gm,''));
 vm.runInNewContext(code,{serve:fn=>handle=fn,Deno:{env:{get:key=>env[key]}},URL,Response,fetch:fetcher,AbortSignal});
 return handle;
}
const body={account_id:'test',recipients:['111'],message:'synthetic',approval_id:'approved'};
const request=(data=body,auth=true)=>new Request('https://edge.example/send-message',{method:'POST',headers:{'Content-Type':'application/json',...(auth?{Authorization:'Bearer test-token'}:{})},body:JSON.stringify(data)});
test('anonymous send rejected',async()=>assert.equal((await handler('send-message')(request(body,false))).status,401));
test('missing runtime is not a fake queue',async()=>{
 const r=await handler('send-message')(request());assert.equal(r.status,503);assert.equal((await r.json()).queued,false);
});
test('forward approved request to runtime and preserve denial',async()=>{
 let called=false;
 const h=handler('send-message',{TELESET_API_ORIGIN:'https://runtime.example'},async(url,init)=>{
  called=true;assert.equal(url.pathname,'/api/bulk/send-personal');assert.equal(init.redirect,'error');
  assert.equal(init.headers.Authorization,'Bearer test-token');assert.equal(JSON.parse(init.body).approval_id,'approved');
  return new Response('{}',{status:403});
 });
 assert.equal((await h(request())).status,403);assert.equal(called,true);
});
test('missing approval never forwarded',async()=>{
 assert.equal((await handler('send-message',{TELESET_API_ORIGIN:'https://runtime.example'})(request({...body,approval_id:null}))).status,400);
});
test('uncertain send timeout never invites blind retry',async()=>{
 const r=await handler('send-message',{TELESET_API_ORIGIN:'https://runtime.example'})(request());
 assert.equal(r.status,502);assert.equal((await r.json()).retry_safe,false);
});
test('legacy edge scheduler cannot mutate or claim execution',async()=>{
 const r=await handler('run-schedules')();assert.equal(r.status,410);assert.equal((await r.json()).executed,0);
});
function worker(fetcher=()=>{throw Error('unexpected network')}) {
 const code=readFileSync('deploy/cloudflare-worker.js','utf8').replace('export default', 'globalThis.worker =');
 const context={Request,Response,URL,fetch:fetcher};vm.runInNewContext(code,context);return context.worker;
}
test('Worker CORS permits known dashboard origins and rejects arbitrary origins',async()=>{
 for(const origin of ['https://open-teleset.site','https://www.open-teleset.site','https://open-teleset-dashboard.pages.dev','https://untrusted.example']) {
  const r=await worker().fetch(new Request('https://edge.example/health',{method:'OPTIONS',headers:{Origin:origin}}),{});
  assert.equal(r.headers.get('Access-Control-Allow-Origin'),origin.endsWith('untrusted.example')?null:origin);
  assert.equal(r.headers.get('Vary'),'Origin');
 }
});
test('Worker cannot report healthy without an origin',async()=>{
 const r=await worker().fetch(new Request('https://edge.example/health'),{});assert.equal(r.status,503);
});
test('Worker preserves the upstream WebSocket response object',async()=>{
 const upgrade={status:101,webSocket:{}};
 const r=await worker(async()=>upgrade).fetch(new Request('https://edge.example/ws'),{ORIGIN:'https://runtime.example'});
 assert.equal(r,upgrade);
});
