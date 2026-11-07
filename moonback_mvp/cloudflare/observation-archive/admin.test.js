import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {readFileSync} from 'node:fs';
import worker,{INSERT} from './src/index.js';
import {LIST_SQL,SUMMARY_SQL,DETAIL_SQL,normalizedTime,encodeCursor} from './src/admin.js';
const WRITE='write-test-only',READ='read-test-only';
const id=n=>'obs-'+n.toString(16).padStart(32,'0');
const event=(n,time='2026-11-07T00:00:00+00:00')=>({event_id:id(n),schema_version:1,time,path:'/journal',method:'GET',status:200,response_completed:1,ua:'Example',agent_like:0});
const compare=(a,b)=>a<b?-1:a>b?1:0;
class DB {
  constructor(rows=[]) {this.rows=rows;this.calls=[];this.fail=false;}
  prepare(sql) {
    assert.ok([LIST_SQL,SUMMARY_SQL,DETAIL_SQL].includes(sql));this.calls.push({sql,args:[]});const call=this.calls.at(-1);
    const first=async()=>{
      if(this.fail)throw new Error('private SQL diagnostic');
      if(sql===SUMMARY_SQL){const t=this.rows.map(x=>normalizedTime(x.time)).sort();return{total:t.length,first_time:t[0]??null,last_time:t.at(-1)??null};}
      return this.rows.find(x=>x.event_id===call.args[0])??null;
    };
    const all=async()=>{
      if(this.fail)throw new Error('private SQL diagnostic');
      const [time,after,limit]=call.args;
      const rows=this.rows.filter(x=>normalizedTime(x.time)>time||(normalizedTime(x.time)===time&&x.event_id>after))
        .sort((a,b)=>compare(normalizedTime(a.time),normalizedTime(b.time))||compare(a.event_id,b.event_id)).slice(0,limit)
        .map(({event_id,time,method,status})=>({event_id,time,method,status}));
      return{success:true,results:rows};
    };
    return{first,bind:(...args)=>{call.args=args;return{first,all};}};
  }
}
const env=(rows=[])=>({OBS_DB:new DB(rows),MOONBACK_OBSERVATION_INGEST_SECRET:WRITE,MOONBACK_OBSERVATION_READ_SECRET:READ});
function req(path='/v1/admin/observations',secret=READ,method='GET'){
  return new Request('https://observation.example'+path,{method,headers:secret?{Authorization:'Bearer '+secret}:{}});
}
function write(secret){const body=JSON.stringify({...event(1),response_completed:true,agent_like:false});return new Request('https://observation.example/v1/observations',{method:'POST',headers:{Authorization:'Bearer '+secret,'Content-Type':'application/json','Content-Length':String(Buffer.byteLength(body))},body});}
test('read auth: missing/wrong/write/unset/empty/same credentials all query zero',async()=>{
  for(const s of [{sent:null},{sent:WRITE},{sent:'wrong'},{read:''},{read:undefined},{read:WRITE,sent:WRITE}]){
    const e=env([event(1)]);if('read'in s)e.MOONBACK_OBSERVATION_READ_SECRET=s.read;
    const r=await worker.fetch(req(undefined,'sent'in s?s.sent:READ),e);
    assert.equal(r.status,404);assert.deepEqual(await r.json(),{error:'not_found'});assert.equal(e.OBS_DB.calls.length,0);
  }
});
test('read secret cannot ingest; same secrets disable both; unset read preserves ingest',async()=>{
  for(const e of [env(),{...env(),MOONBACK_OBSERVATION_READ_SECRET:WRITE}]){
    assert.equal((await worker.fetch(write(e.MOONBACK_OBSERVATION_READ_SECRET),e)).status,404);assert.equal(e.OBS_DB.calls.length,0);
  }
  const e=env();delete e.MOONBACK_OBSERVATION_READ_SECRET;let writes=0;
  e.OBS_DB.prepare=sql=>{assert.equal(sql,INSERT);return{bind:()=>({run:async()=>{writes++;return{success:true,meta:{changes:1}};}})};};
  assert.equal((await worker.fetch(write(WRITE),e)).status,201);assert.equal(writes,1);
});
test('summary is saved count/range only, empty or mixed UTC formats',async()=>{
  for(const rows of [[],[event(1)],[event(1),event(2,'2026-11-07T00:00:00.1Z')]]){
    const e=env(rows),r=await worker.fetch(req('/v1/admin/observations/summary'),e);assert.equal(r.status,200);
    const d=await r.json();assert.deepEqual(Object.keys(d).sort(),['first_time','last_time','total']);assert.equal(d.total,rows.length);
    assert.equal(d.first_time,rows.length?'2026-11-07T00:00:00.000000Z':null);
    assert.equal(d.last_time,rows.length===2?'2026-11-07T00:00:00.100000Z':d.first_time);
    assert.deepEqual(e.OBS_DB.calls.map(x=>x.sql),[SUMMARY_SQL]);
  }
});
test('keyset pages normalize equivalent timestamps; summaries exclude path/UA',async()=>{
  const rows=[event(3,'2026-11-07T00:00:00.000001Z'),event(2,'2026-11-07T00:00:00.000000+00:00'),event(1,'2026-11-07T00:00:00Z')];
  const e=env(rows),a=await(await worker.fetch(req('/v1/admin/observations?limit=2'),e)).json();
  assert.deepEqual(a.items.map(x=>x.event_id),[id(1),id(2)]);
  assert.deepEqual(Object.keys(a.items[0]).sort(),['event_id','method','status','time']);
  assert.equal(a.next_cursor,encodeCursor(['2026-11-07T00:00:00.000000Z',id(2)]));
  const b=await(await worker.fetch(req('/v1/admin/observations?limit=2&cursor='+a.next_cursor),e)).json();
  assert.deepEqual(b.items.map(x=>x.event_id),[id(3)]);assert.equal(b.next_cursor,null);
  assert.equal(e.OBS_DB.calls[1].args[2],3);
});
test('default20/max50/empty list and response size bounds',async()=>{
  for(const [query,count]of [['',20],['?limit=50',50]]){
    const r=await worker.fetch(req('/v1/admin/observations'+query),env(Array.from({length:51},(_,n)=>event(n+1))));
    const body=await r.text();assert.equal(JSON.parse(body).items.length,count);assert.ok(Buffer.byteLength(body)<=65536);assert.ok(!body.includes('Example'));
  }
  assert.deepEqual(await(await worker.fetch(req(),env())).json(),{items:[],next_cursor:null});
});
test('invalid filters/IDs/SQL injection/cursors query zero',async()=>{
  const paths=['?limit=0','?limit=51','?limit=1&limit=2','?unknown=1','?status=200','?limit=1%20OR%201=1','?%6cimit=1','?cursor=','?cursor=bad','?cursor='+('a'.repeat(600)),'/bad','/'+id(1)+'/extra','/'+id(1)+'?limit=1','/summary?limit=1'];
  for(const t of ['2026-02-30T00:00:00.000000Z','2026-11-07T24:00:00.000000Z','2026-11-07T00:00:00Z'])paths.push('?cursor='+encodeCursor([t,id(1)]));
  for(const p of paths){const e=env(),r=await worker.fetch(req('/v1/admin/observations'+p),e);assert.equal(r.status,400,p);assert.equal(e.OBS_DB.calls.length,0);}
});
test('explicit detail only, no dedup hash, missing ID404, no-store',async()=>{
  const row={...event(1),ua:'untrusted\u001b[2J\u202e',payload_hash:'internal-only'};
  const e=env([row]),r=await worker.fetch(req('/v1/admin/observations/'+id(1)),e);assert.equal(r.status,200);
  const d=await r.json();assert.equal(d.ua,row.ua);assert.equal(d.response_completed,true);assert.equal(d.agent_like,false);assert.ok(!('payload_hash'in d));
  assert.equal(r.headers.get('Cache-Control'),'no-store');assert.equal((await worker.fetch(req('/v1/admin/observations/'+id(2)),e)).status,404);
});
test('HEAD body empty; management write methods never query',async()=>{
  const r=await worker.fetch(req(undefined,READ,'HEAD'),env([event(1)]));assert.equal(r.status,200);assert.equal(await r.text(),'');assert.ok(Number(r.headers.get('Content-Length'))>0);
  for(const m of ['POST','PUT','PATCH','DELETE','OPTIONS']){const e=env();assert.equal((await worker.fetch(req(undefined,READ,m),e)).status,404);assert.equal(e.OBS_DB.calls.length,0);}
});
test('D1 failure and oversized/malformed rows fixed503 without reflection',async()=>{
  const e=env();e.OBS_DB.fail=true;
  for(const p of ['', '/summary','/'+id(1)]){const r=await worker.fetch(req('/v1/admin/observations'+p),e);assert.equal(r.status,503);assert.deepEqual(await r.json(),{error:'storage_unavailable'});}
  assert.equal((await worker.fetch(req('/v1/admin/observations/'+id(1)),env([{...event(1),ua:'x'.repeat(100000)}]))).status,503);
});
test('read burst limit does not affect ingest',async()=>{
  const e=env();for(let n=0;n<30;n++)assert.equal((await worker.fetch(req(),e)).status,200);
  const count=e.OBS_DB.calls.length;assert.equal((await worker.fetch(req(),e)).status,429);assert.equal(e.OBS_DB.calls.length,count);
  e.OBS_DB.prepare=sql=>{assert.equal(sql,INSERT);return{bind:()=>({run:async()=>({success:true,meta:{changes:1}})})};};
  assert.equal((await worker.fetch(write(WRITE),e)).status,201);
});
test('management has no mutation SQL, logging or outbound fetch',async()=>{
  for(const sql of [LIST_SQL,SUMMARY_SQL,DETAIL_SQL])assert.match(sql,/^SELECT /);
  const saved=[globalThis.fetch,console.log,console.warn,console.error];let calls=0;
  globalThis.fetch=()=>{calls++;throw new Error('no fetch');};console.log=console.warn=console.error=()=>{calls++;};
  try{for(const p of ['', '/summary','/'+id(1)])assert.equal((await worker.fetch(req('/v1/admin/observations'+p),env([event(1)]))).status,200);assert.equal(calls,0);}
  finally{[globalThis.fetch,console.log,console.warn,console.error]=saved;}
});
test('actual SQLite fixed queries/index, microsecond pagination and no changed rows',()=>{
  const migration=['0001_observations.sql','0002_history_index.sql'].map(x=>readFileSync(new URL('./migrations/'+x,import.meta.url),'utf8')).join('\n');
  const program=`import json,sqlite3,sys\nx=json.load(sys.stdin)\nc=sqlite3.connect(':memory:')\nc.row_factory=sqlite3.Row\nc.executescript(x['migration'])\nfor row in x['rows']: c.execute(x['insert'],[row[k] for k in x['fields']]+['hash'])\nc.commit()\nbefore=[tuple(r) for r in c.execute('SELECT * FROM observation_events ORDER BY event_id')]\ns=dict(c.execute(x['summary']).fetchone())\nassert s=={'total':3,'first_time':'2026-11-07T00:00:00.000000Z','last_time':'2026-11-07T00:00:00.000001Z'}\np=list(c.execute(x['list'],['0001-01-01T00:00:00.000000Z','',2]))\nassert [r['event_id'] for r in p]==[x['rows'][1]['event_id'],x['rows'][2]['event_id']]\nq=list(c.execute(x['list'],['2026-11-07T00:00:00.000000Z',p[-1]['event_id'],2]))\nassert len(q)==1 and q[0]['event_id']==x['rows'][0]['event_id']\nassert c.execute(x['detail'],[q[0]['event_id']]).fetchone()['ua']=='Example'\nassert any('idx_observation_history' in r[3] for r in c.execute('EXPLAIN QUERY PLAN '+x['list'],['0001-01-01T00:00:00.000000Z','',2]))\nassert before==[tuple(r) for r in c.execute('SELECT * FROM observation_events ORDER BY event_id')]\n`;
  const rows=[event(3,'2026-11-07T00:00:00.000001Z'),event(1,'2026-11-07T00:00:00Z'),event(2,'2026-11-07T00:00:00.000000+00:00')];
  const fields=['event_id','schema_version','time','path','method','status','response_completed','ua','agent_like'];
  const r=spawnSync('python3',['-c',program],{input:JSON.stringify({migration,rows,fields,insert:INSERT,list:LIST_SQL,summary:SUMMARY_SQL,detail:DETAIL_SQL}),encoding:'utf8'});assert.equal(r.status,0,r.stderr);
});
test('actual Worker JSON is accepted by Python client and sensitive terminal text redacted',async()=>{
  const row={...event(1),ua:'Authorization: Bearer synthetic-credential-12345',path:'/journal\u001b[2J\u202e'};
  const fixtures=[];
  for(const path of ['', '/summary','/'+id(1)]){
    const r=await worker.fetch(req('/v1/admin/observations'+path),env([row]));assert.equal(r.status,200);
    fixtures.push({path:'/v1/admin/observations'+(path||'?limit=20'),status:r.status,body:await r.text()});
  }
  const program=`import json,sys\nfrom unittest.mock import patch\nsys.path.insert(0,sys.argv[1])\nfrom observation_admin import ObservationAdminClient,safe_text,ADMIN_URL\nx=json.load(sys.stdin)\nfixtures={v['path']:v for v in x}\ndef transport(path,headers):\n assert headers['Authorization']=='Bearer read-test-only'\n v=fixtures[path]\n return v['status'],v['body'].encode()\nwith patch('socket.create_connection',side_effect=AssertionError('network prohibited')):\n c=ObservationAdminClient('read-test-only',url=ADMIN_URL,transport=transport)\n assert len(c.list_records()['items'])==1\n assert c.summary()['total']==1\n d=c.get_record('obs-'+format(1,'032x'))\n assert safe_text(d['ua'])=='[redacted:credential]'\n assert chr(27) not in safe_text(d['path']) and chr(0x202e) not in safe_text(d['path'])\n c.close()\n`;
  const app=new URL('../../',import.meta.url);
  const r=spawnSync('python3',['-c',program,decodeURIComponent(app.pathname)],{input:JSON.stringify(fixtures),encoding:'utf8'});
  assert.equal(r.status,0,r.stderr);assert.equal(r.stdout,'');
});
