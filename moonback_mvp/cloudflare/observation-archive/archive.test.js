import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {webcrypto} from 'node:crypto';
import worker, {INSERT,LOOKUP,strictObject,validate} from './src/index.js';
if (!globalThis.crypto) globalThis.crypto = webcrypto;
const SECRET = 'test-only-not-production';
const base = () => ({event_id:'obs-'+'a'.repeat(32),schema_version:1,
  time:'2026-11-07T00:00:00+00:00',path:'/journal',method:'GET',status:200,
  response_completed:true,ua:'ExampleClient/1.0',agent_like:false});
class FakeDB {
  constructor() { this.rows = new Map(); this.calls=[]; }
  prepare(sql) {
    assert.ok([INSERT,LOOKUP].includes(sql)); this.calls.push(sql);
    return {bind:(...values) => ({
      run:async()=>{
        assert.equal(sql,INSERT); const exists=this.rows.has(values[0]);
        if (!exists) this.rows.set(values[0],values);
        return {success:true,meta:{changes:exists?0:1}};
      },
      first:async()=>{
        assert.equal(sql,LOOKUP); const row=this.rows.get(values[0]);
        return row?{payload_hash:row.at(-1)}:null;
      },
    })};
  }
}
function req({event=base(),raw,auth=true,method='POST',path='/v1/observations',headers={}}={}) {
  const body=raw??JSON.stringify(event);
  return new Request('https://observation.example'+path,{method,
    headers:{'Content-Type':'application/json','Content-Length':String(Buffer.byteLength(body)),
      ...(auth?{Authorization:'Bearer '+SECRET}:{}),...headers},
    ...(['GET','HEAD'].includes(method)?{}:{body})});
}
function env(db=new FakeDB()) {return {OBS_DB:db,MOONBACK_OBSERVATION_INGEST_SECRET:SECRET};}

test('authenticated commit before ack; no body reflection',async()=>{
  const e=env(); const r=await worker.fetch(req(),e);
  assert.equal(r.status,201);
  assert.deepEqual(await r.json(),{status:'stored',event_id:base().event_id});
  assert.equal(e.OBS_DB.rows.size,1); assert.equal(r.headers.get('Cache-Control'),'no-store');
  assert.equal(r.headers.get('Access-Control-Allow-Origin'),null);
});
test('identical replay with different key order creates one row',async()=>{
  const e=env(); await worker.fetch(req(),e);
  assert.equal((await worker.fetch(req({event:Object.fromEntries(Object.entries(base()).reverse())}),e)).status,201);
  assert.equal(e.OBS_DB.rows.size,1);
});
test('ID collision returns409 without overwrite',async()=>{
  const e=env(); await worker.fetch(req(),e); const original=[...e.OBS_DB.rows.values()][0];
  const r=await worker.fetch(req({event:{...base(),path:'/archive'}}),e);
  assert.equal(r.status,409); assert.deepEqual(await r.json(),{error:'conflict'});
  assert.deepEqual([...e.OBS_DB.rows.values()][0],original);
});
test('missing/wrong/unset/empty auth: zero queries',async()=>{
  for (const setting of [{request:req({auth:false})},{request:req({headers:{Authorization:'Bearer wrong'}})}, {secret:''},{secret:undefined}]) {
    const e=env(); if ('secret' in setting) e.MOONBACK_OBSERVATION_INGEST_SECRET=setting.secret;
    const r=await worker.fetch(setting.request??req(),e);
    assert.equal(r.status,404); assert.equal(e.OBS_DB.calls.length,0);
    assert.deepEqual(await r.json(),{error:'not_found'});
  }
});
test('read/admin/response routes do not exist',async()=>{
  for (const options of [{method:'GET'},{method:'HEAD'},{method:'OPTIONS'}, {path:'/v1/observations/'},
    {path:'/v1/observations?x=1'},{path:'/api/responses/status'},{path:'/v1/responses'},{path:'/v1/admin/responses'}]) {
    const e=env(); assert.equal((await worker.fetch(req(options),e)).status,404);
    assert.equal(e.OBS_DB.calls.length,0);
  }
});
test('content type/length/actual size/chunked rejected',async()=>{
  for (const headers of [{'Content-Type':'text/plain'},{'Content-Length':'0'}, {'Content-Length':'9000'},
    {'Content-Length':'1'},{'Transfer-Encoding':'chunked'}]) {
    const e=env(); assert.equal((await worker.fetch(req({headers}),e)).status,400); assert.equal(e.OBS_DB.calls.length,0);
  }
  const noLength=req(); noLength.headers.delete('Content-Length');
  assert.equal((await worker.fetch(noLength,env())).status,400);
  assert.equal((await worker.fetch(req({raw:'x'.repeat(9000),headers:{'Content-Length':'100'}}),env())).status,400);
});
test('duplicate/escaped keys, nested/unknown fields and invalid JSON rejected',async()=>{
  for (const raw of ['{}','{','[]','{\u00a0"x":1}','{"x":1,"x":2}','{"x":1,"\\u0078":2}',
    '{"x":'+ '['.repeat(5000) + ']'.repeat(5000)+'}',JSON.stringify({...base(),ip:'not allowed'})]) {
    const e=env(); assert.equal((await worker.fetch(req({raw}),e)).status,400); assert.equal(e.OBS_DB.calls.length,0);
  }
  assert.throws(()=>strictObject('{"x":"unterminated}'));
});
test('dates/types/lengths/control/query validation',async()=>{
  for (const [key,value] of [['status',true],['schema_version',true],['status',600],['ua','x'.repeat(181)],
    ['ua','\u202e'],['time','2026-02-30T00:00:00Z'],['time','2026-11-07T24:00:00Z'],
    ['method','POST'],['path','/x?q=1'],['event_id','../bad'],['response_completed',1],['agent_like',null]]) {
    const e=env(); assert.equal((await worker.fetch(req({event:{...base(),[key]:value}}),e)).status,400,key);
    assert.equal(e.OBS_DB.calls.length,0);
  }
  assert.equal(validate({...base(),status:null,response_completed:false}),true);
});
test('secret-like values not stored/reflected; public email accepted',async()=>{
  const sensitive='Authorization: Bearer synthetic-credential-12345';
  const e=env(); const r=await worker.fetch(req({event:{...base(),ua:sensitive}}),e);
  assert.equal(r.status,400); assert.equal(e.OBS_DB.calls.length,0); assert.equal((await r.text()).includes(sensitive),false);
  assert.equal(validate({...base(),ua:'Public hello@example.com basic implementation'}),true);
});
test('unknown D1 quota/capacity failures remain safe503, never acknowledged',async()=>{
  for (const diagnostic of ['database unavailable','quota exceeded','database or disk is full',
    'SQLITE_FULL', 'D1_ERROR: SQLITE_FULL', 'D1_ERROR: quota exceeded',
    'D1_ERROR: Exceeded maximum DB size',
    'SQL: SELECT "Exceeded maximum DB size."',
    'D1_ERROR: Exceeded maximum DB size. private trailing detail',
    'D1_ERROR: Your account has exceeded D1\'s free tier daily row write limit.',
    'private Authorization: Bearer synthetic-credential-do-not-reflect']) {
    const e=env(); e.OBS_DB.prepare=()=>({bind:()=>({run:async()=>{throw new Error(diagnostic);}})});
    const r=await worker.fetch(req(),e); assert.equal(r.status,503); assert.deepEqual(await r.json(),{error:'storage_unavailable'});
  }
});
test('exact documented D1 capacity errors return fixed507 without logging',async()=>{
  const documented = [
    'Exceeded maximum DB size.',
    "Your account has exceeded D1's maximum account storage limit, please contact Cloudflare to raise your limit",
    "Your account has exceeded D1's free tier daily row read limit. Upgrade to a paid plan or wait until tomorrow (midnight UTC) to continue. See https://developers.cloudflare.com/d1/platform/limits/ for more details.",
    "Your account has exceeded D1's free tier daily row write limit. Upgrade to a paid plan or wait until tomorrow (midnight UTC) to continue. See https://developers.cloudflare.com/d1/platform/limits/ for more details.",
  ];
  const saved = [console.log, console.error, console.warn]; let logs=0;
  console.log=console.error=console.warn=()=>{logs++;};
  try {
    for (const message of documented) for (const prefix of ['', 'D1_ERROR: ']) {
      const e=env(); let calls=0;
      e.OBS_DB.prepare=()=>({bind:()=>({run:async()=>{calls++; throw new Error(prefix+message);}})});
      const r=await worker.fetch(req(),e);
      assert.equal(r.status,507);
      assert.deepEqual(await r.json(),{error:'capacity'});
      assert.equal(calls,1); assert.equal(e.OBS_DB.rows.size,0);
    }
    assert.equal(logs,0);
  } finally {[console.log,console.error,console.warn]=saved;}
});
test('unknown error shapes and unsuccessful D1 results do not infer capacity',async()=>{
  const failureShapes=[null,'D1_ERROR: Exceeded maximum DB size.',
    {code:'SQLITE_FULL'}, new Error('unknown',{cause:new Error('D1_ERROR: Exceeded maximum DB size.')}),
    {get message(){throw new Error('private getter failure');}}];
  for (const failure of failureShapes) {
    const e=env();
    e.OBS_DB.prepare=()=>({bind:()=>({run:async()=>{throw failure;}})});
    const r=await worker.fetch(req(),e);
    assert.equal(r.status,503); assert.deepEqual(await r.json(),{error:'storage_unavailable'});
  }
  const e=env();
  e.OBS_DB.prepare=()=>({bind:()=>({run:async()=>({success:false,error:'D1_ERROR: Exceeded maximum DB size.'})})});
  const r=await worker.fetch(req(),e);
  assert.equal(r.status,503); assert.deepEqual(await r.json(),{error:'storage_unavailable'});
});
test('capacity failure during duplicate lookup never yields a false stored ack',async()=>{
  const e=env(); await worker.fetch(req(),e);
  const original=e.OBS_DB.prepare.bind(e.OBS_DB);
  e.OBS_DB.prepare=(sql)=>sql===LOOKUP
    ?{bind:()=>({first:async()=>{throw new Error('D1_ERROR: Exceeded maximum DB size.');}})}
    :original(sql);
  const r=await worker.fetch(req(),e);
  assert.equal(r.status,507); assert.deepEqual(await r.json(),{error:'capacity'});
  assert.equal(e.OBS_DB.rows.size,1);
});
test('Worker507 reaches Python capacity without retry;503 retains bounded retries',async()=>{
  // Execute the unchanged production transport/queue with this actual Worker
  // response as its fake HTTP response. No localhost or external sockets.
  const program = `import io,json,sys,time\nfrom urllib.error import HTTPError\nfrom unittest.mock import patch\nsys.path.insert(0,sys.argv[1])\nfrom observation_archive import ObservationTransport,ObservationArchive\nx=json.load(sys.stdin)\nclass FakeOpener:\n def __init__(self): self.calls=0\n def open(self,*args,**kwargs):\n  self.calls+=1\n  raise HTTPError('https://observation.example/v1/observations',x['status'],'synthetic',{},io.BytesIO(x['body'].encode()))\nwith patch('socket.create_connection',side_effect=AssertionError('network prohibited')):\n t=ObservationTransport('https://observation.example/v1/observations','test-only')\n t._opener=FakeOpener()\n a=ObservationArchive(t,max_attempts=3,retry_seconds=0)\n try:\n  assert a.submit(x['event'])\n  a.start()\n  until=time.monotonic()+2\n  while a.health().get('failed',0)!=1 and time.monotonic()<until: time.sleep(.005)\n  h=a.health()\n  assert h.get('failed')==1 and h.get('stored',0)==0 and h['pending']==0\n  assert h['last_error']==x['category']\n  assert t._opener.calls==x['attempts']\n finally: a.close()\n`;
  for (const [message,status,category,attempts] of [
    ['D1_ERROR: Exceeded maximum DB size.',507,'capacity',1],
    ['D1_ERROR: unknown quota failure',503,'upstream_unavailable',3],
  ]) {
    const e=env(); e.OBS_DB.prepare=()=>({bind:()=>({run:async()=>{throw new Error(message);}})});
    const response=await worker.fetch(req(),e);
    assert.equal(response.status,status);
    const r=spawnSync('python3',['-c',program,fileURLToPath(new URL('../../',import.meta.url))],{
      input:JSON.stringify({status:response.status,body:await response.text(),event:base(),category,attempts}),encoding:'utf8',timeout:5000,
    });
    assert.equal(r.status,0,r.stderr);
    assert.equal(r.stdout,'');
  }
});
test('actual SQLite migration and bind SQL enforce replay/conflict',()=>{
  const migration=readFileSync(new URL('./migrations/0001_observations.sql',import.meta.url),'utf8');
  const program=`import json,sqlite3,sys\nx=json.load(sys.stdin)\nc=sqlite3.connect(':memory:')\nc.executescript(x['migration'])\nv=x['values']\nc.execute(x['insert'],v)\nassert c.execute('SELECT count(*) FROM observation_events').fetchone()[0]==1\nc.execute(x['insert'],v)\nassert c.execute('SELECT count(*) FROM observation_events').fetchone()[0]==1\nv[-1]='different'\nc.execute(x['insert'],v)\nassert c.execute(x['lookup'],[v[0]]).fetchone()[0]=='hash'\nassert c.execute('SELECT count(*) FROM sqlite_master WHERE name="responses"').fetchone()[0]==0\n`;
  const values=[base().event_id,1,base().time,'/journal','GET',200,1,'Example',0,'hash'];
  const r=spawnSync('python3',['-c',program],{input:JSON.stringify({migration,insert:INSERT,lookup:LOOKUP,values}),encoding:'utf8'});
  assert.equal(r.status,0,r.stderr);
});
test('worker performs no outbound fetch or logging',async()=>{
  const saved=[globalThis.fetch,console.log,console.error]; let calls=0;
  globalThis.fetch=()=>{calls++;throw new Error('network prohibited');}; console.log=console.error=()=>{calls++;};
  try {assert.equal((await worker.fetch(req(),env())).status,201);assert.equal(calls,0);}
  finally {[globalThis.fetch,console.log,console.error]=saved;}
});
test('invalid UTF-8 bytes rejected before query',async()=>{
  const e=env();
  const r=await worker.fetch(new Request('https://observation.example/v1/observations',{
    method:'POST',headers:{Authorization:'Bearer '+SECRET,'Content-Type':'application/json','Content-Length':'1'},
    body:new Uint8Array([255]),
  }),e);
  assert.equal(r.status,400); assert.equal(e.OBS_DB.calls.length,0);
});
