import test from 'node:test';
import assert from 'node:assert/strict';
import { handleRequest } from '../src/index.js';
const ADMIN = 'synthetic-admin-auth';
const INTAKE = 'synthetic-intake-auth';
const row = (id, status = 'pending') => ({id, journal_id: 'journal-test', received_at: '2026-10-10T00:00:00Z', agent_name: 'Claimed Agent', response: 'Untrusted text', source_url: null, status});
// IDs and timestamps are ASCII; code-unit order equals SQLite UTF-8 BINARY.
const binaryCompare = (a, b) => a < b ? -1 : a > b ? 1 : 0;
class DB {
  constructor(rows = [], fail = false) { this.rows = rows; this.fail = fail; this.calls = []; }
  prepare(sql) {
    assert.match(sql, /^SELECT /);
    const db = this;
    let args;
    return {
      bind(...values) { args = values; db.calls.push({sql, args}); return this; },
      async all() {
        if (db.fail) throw Error('private row error');
        let selected = db.rows.filter(r => r.status === 'pending').sort((a,b) => binaryCompare(a.received_at,b.received_at) || binaryCompare(a.id,b.id));
        if (args.length === 3) selected = selected.filter(r => r.received_at > args[0] || (r.received_at === args[0] && r.id > args[1]));
        return {success:true,results:selected.slice(0,args.at(-1))};
      },
      async first() {
        if (db.fail) throw Error('private row error');
        return db.rows.find(r => r.id === args[0] && r.status === 'pending') || null;
      },
      run() { assert.fail('D1 write forbidden'); },
    };
  }
}
async function run(path = '/v1/admin/responses', {secret = ADMIN, db = new DB(), method = 'GET', admin = ADMIN, intake = INTAKE} = {}) {
  const headers = secret === null ? {} : {Authorization: 'Bearer ' + secret};
  const response = await handleRequest(new Request('https://example.invalid' + path, {method,headers}), {DB:db,MOONBACK_INTAKE_SECRET:intake,MOONBACK_RESPONSE_ADMIN_SECRET:admin});
  const text = await response.text();
  return {response, data:text ? JSON.parse(text) : null, db};
}
test('admin auth rejects missing, wrong, intake, unset and same secrets before DB', async () => {
  for (const config of [{secret:null},{secret:'wrong'},{secret:INTAKE},{admin:undefined},{admin:INTAKE,secret:INTAKE}]) {
    // undefined is supplied explicitly below because defaults are for normal cases.
    if ('admin' in config && config.admin === undefined) config.admin = '';
    const r = await run(undefined,config);
    assert.equal(r.response.status,404); assert.deepEqual(r.data,{error:'not_found'}); assert.equal(r.db.calls.length,0);
  }
});
test('admin credential cannot authorize intake or internal public status', async () => {
  for (const [path,method] of [['/v1/responses','POST'],['/v1/responses/status','GET']]) {
    const r = await run(path,{method}); assert.equal(r.response.status,404); assert.equal(r.db.calls.length,0);
  }
});
test('pending list is bounded, projected, ordered and paginated with bind parameters', async () => {
  const db = new DB([row('response-c'),row('response-b'),row('response-a'),row('response-d','published'),row('response-e','rejected')]);
  const first = await run('/v1/admin/responses?limit=2',{db});
  assert.equal(first.response.status,200);
  assert.deepEqual(first.data.items.map(r=>r.id),['response-a','response-b']);
  assert.deepEqual(Object.keys(first.data.items[0]).sort(),['id','journal_id','received_at']);
  assert.ok(first.data.next_cursor);
  const second = await run('/v1/admin/responses?limit=2&cursor='+first.data.next_cursor,{db});
  assert.deepEqual(second.data.items.map(r=>r.id),['response-c']); assert.equal(second.data.next_cursor,null);
  assert.deepEqual(db.calls[1].args,['2026-10-10T00:00:00Z','response-b',3]);
});
test('default and maximum list limits', async () => {
  const db = new DB(Array.from({length:60},(_,i)=>row('response-'+String(i).padStart(2,'0'))));
  assert.equal((await run(undefined,{db})).data.items.length,20);
  assert.equal((await run('/v1/admin/responses?limit=50',{db})).data.items.length,50);
});
test('pagination uses SQLite BINARY order for punctuation, digits and letters', async () => {
  const ids = ['response-a-', 'response-a0', 'response-a_', 'response-aa'];
  const db = new DB([...ids].reverse().map(id => row(id)));
  const first = await run('/v1/admin/responses?limit=2', {db});
  assert.deepEqual(first.data.items.map(item => item.id), ids.slice(0, 2));
  const expected = btoa(JSON.stringify([row(ids[1]).received_at, ids[1]]))
    .replaceAll('+','-').replaceAll('/','_').replace(/=+$/,'');
  assert.equal(first.data.next_cursor, expected);
  const second = await run('/v1/admin/responses?limit=2&cursor=' + expected, {db});
  assert.deepEqual(second.data.items.map(item => item.id), ids.slice(2));
  assert.equal(second.data.next_cursor, null);
  assert.deepEqual([...first.data.items, ...second.data.items].map(item => item.id), ids);
});
test('impossible dates and non-UTC or over-precise cursors fail before D1', async () => {
  const invalid = ['2026-02-30T00:00:00Z', '2025-02-29T00:00:00Z',
    '2026-04-31T00:00:00Z', '2026-00-01T00:00:00Z', '2026-13-01T00:00:00Z',
    '2026-01-00T00:00:00Z', '0000-01-01T00:00:00Z', '2026-01-01T24:00:00Z',
    '2026-01-01T00:60:00Z', '2026-01-01T00:00:60Z',
    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00z',
    '2026-01-01T00:00:00.Z', '2026-01-01T00:00:00.1234567Z',
    '2026-01-01T00:00:00Z\n'];
  for (const timestamp of invalid) {
    const cursor = btoa(JSON.stringify([timestamp, 'response-a']))
      .replaceAll('+','-').replaceAll('/','_').replace(/=+$/,'');
    const result = await run('/v1/admin/responses?cursor=' + cursor);
    assert.equal(result.response.status, 400, timestamp);
    assert.deepEqual(result.data, {error:'invalid_query'});
    assert.equal(result.db.calls.length, 0);
  }
});
test('real leap dates and existing UTC fractional precision remain valid', async () => {
  for (const timestamp of ['2024-02-29T23:59:59Z', '0099-01-01T00:00:00Z',
    '2026-01-01T00:00:00.1Z', '2026-01-01T00:00:00.123456Z']) {
    const cursor = btoa(JSON.stringify([timestamp, 'response-a']))
      .replaceAll('+','-').replaceAll('/','_').replace(/=+$/,'');
    const result = await run('/v1/admin/responses?cursor=' + cursor);
    assert.equal(result.response.status, 200, timestamp);
    assert.equal(result.db.calls.length, 1);
  }
});
test('pending detail projects only draft fields with status', async () => {
  const db = new DB([{...row('response-a'),response:'<script>\u001b[31m\u202e text', arbitrary:'not returned'}]);
  const r = await run('/v1/admin/responses/response-a',{db});
  assert.equal(r.response.status,200); assert.equal(r.data.status,'pending');
  assert.equal(r.data.item.source_url,''); assert.equal(r.data.item.arbitrary,undefined);
  assert.equal(r.response.headers.get('Cache-Control'),'no-store');
});
test('missing, published and rejected details are unavailable', async () => {
  const db = new DB([row('response-b','published'),row('response-c','rejected')]);
  for (const id of ['response-a','response-b','response-c']) assert.equal((await run('/v1/admin/responses/'+id,{db})).response.status,404);
});
test('invalid query and ID rejected without query', async () => {
  for (const path of ['/v1/admin/responses?limit=0','/v1/admin/responses?limit=51','/v1/admin/responses?limit=1&limit=2','/v1/admin/responses?sql=SELECT','/v1/admin/responses?cursor=bad','/v1/admin/responses/UPPER','/v1/admin/responses/response-a?limit=1','/v1/admin/responses/response-a/']) {
    const r = await run(path); assert.ok([400,404].includes(r.response.status)); assert.equal(r.db.calls.length,0);
  }
});
test('management write methods rejected; HEAD has no body', async () => {
  for (const method of ['POST','PUT','DELETE','PATCH','OPTIONS']) {
    const r = await run(undefined,{method}); assert.equal(r.response.status,404); assert.equal(r.db.calls.length,0);
  }
  const r = await run(undefined,{method:'HEAD'}); assert.equal(r.response.status,200); assert.equal(r.data,null);
});
test('D1 failure and malformed rows produce fixed safe 503', async () => {
  for (const config of [{db:new DB([],true)},{db:new DB([{...row('response-a'),id:'INVALID'}])}]) {
    const r = await run(undefined,config); assert.equal(r.response.status,503); assert.deepEqual(r.data,{error:'storage_unavailable'});
  }
  const r = await run('/v1/admin/responses/response-a',{db:new DB([{...row('response-a'),response:'x'.repeat(2001)}])});
  assert.equal(r.response.status,503);
});
test('management performs no fetch or custom logs and emits bounded JSON', async () => {
  const savedFetch = globalThis.fetch; const savedLog = console.log;
  globalThis.fetch = () => assert.fail('additional fetch forbidden'); console.log = () => assert.fail('logging forbidden');
  try {
    const r = await run('/v1/admin/responses/response-a',{db:new DB([row('response-a')])});
    assert.equal(r.response.status,200); assert.ok(JSON.stringify(r.data).length < 65536);
    assert.equal(r.db.calls.length,1);
  } finally {globalThis.fetch=savedFetch;console.log=savedLog;}
});

test('admin response path has no dependency on generation or crawler and no mutation SQL', async () => {
  const {readFileSync} = await import('node:fs');
  const source = readFileSync(new URL('../src/admin.js',import.meta.url),'utf8');
  assert.doesNotMatch(source,/\b(?:INSERT INTO|UPDATE responses|DELETE FROM|fetch\(|console\.)/);
  assert.doesNotMatch(source,/journal_openai|crawler|generate_journal_entry/);
  const db=new DB([row('response-a')]);
  await run(undefined,{db});await run('/v1/admin/responses/response-a',{db});
  for (const call of db.calls) assert.match(call.sql,/^SELECT /);
});

test('management limiter bounds reads and expires without storing identity', async () => {
  const {AdminReadLimiter}=await import('../src/admin.js');
  const limiter=new AdminReadLimiter(2,1000);
  assert.equal(limiter.allow(0),true);assert.equal(limiter.allow(1),true);
  assert.equal(limiter.allow(2),false);assert.equal(limiter.allow(1000),true);
  assert.ok(limiter.events.every(value=>typeof value==='number'));
});
