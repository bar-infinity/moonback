import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { handleRequest, PublicStatusCache } from '../src/index.js';

const cursorFor = id => 'sha256:' + createHash('sha256').update(id).digest('hex');
class StatusDB {
  constructor(pending = 0, latest = null) {
    this.pending = pending; this.latest = latest; this.queries = []; this.fail = false;
  }
  prepare(sql) {
    assert.match(sql, /^SELECT EXISTS/);
    assert.match(sql, /ORDER BY rowid DESC LIMIT 1/);
    this.queries.push(sql);
    return { first: async () => {
      if (this.fail) throw Error('private provider details');
      return {pending: this.pending, latest_id: this.latest};
    }};
  }
}
const request = (path = '/api/responses/status', options = {}) =>
  new Request('https://example.invalid' + path, options);
const signal = (pending = false, cursor = null) => new Response(
  JSON.stringify({pending, cursor}), {status: 200});

test('public status requires no secrets and exposes only the anonymous signal', async () => {
  for (const [pending, latest] of [[0, null], [1, 'response-a'], [0, 'response-a'], [1, 'response-b']]) {
    const db = new StatusDB(pending, latest);
    const response = await handleRequest(request(), {DB: db});
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), {pending: Boolean(pending), cursor: latest ? cursorFor(latest) : null});
    assert.equal(db.queries.length, 1);
    assert.match(response.headers.get('Content-Type'), /^application\/json/);
    assert.match(response.headers.get('Cache-Control'), /^public, max-age=\d+, must-revalidate$/);
    assert.equal(response.headers.get('Access-Control-Allow-Origin'), null);
    assert.equal(response.headers.get('Access-Control-Allow-Credentials'), null);
  }
});

test('public status uses exact GET path and rejects query, fragments and write methods', async () => {
  const db = new StatusDB();
  for (const [path, method] of [['/api/responses/status/', 'GET'], ['/api/responses/status?x=1', 'GET'],
    ['/api/responses/status#x', 'GET'], ...['HEAD', 'POST', 'PUT', 'DELETE', 'OPTIONS'].map(method => ['/api/responses/status', method])]) {
    const response = await handleRequest(request(path, {method}), {DB: db});
    assert.equal(response.status, 404);
  }
  assert.equal(db.queries.length, 0);
});

test('public cache coalesces a concurrent burst into one read and clones responses', async () => {
  const db = new StatusDB(1, 'response-private-id');
  const responses = await Promise.all(Array.from({length: 100}, () => handleRequest(request(), {DB: db})));
  for (const response of responses) {
    const text = await response.text();
    assert.equal(response.status, 200);
    assert.doesNotMatch(text, /response-private-id|agent_name|source_url|received_at|journal_id|Authorization/);
    assert.deepEqual(JSON.parse(text), {pending: true, cursor: cursorFor('response-private-id')});
  }
  assert.equal(db.queries.length, 1);
  await handleRequest(request(), {DB: db});
  assert.equal(db.queries.length, 1);
});

test('30-second expiration reflects INSERT but status changes alone preserve cursor', async () => {
  let now = 0, pending = true, latest = 'response-a', reads = 0;
  const cache = new PublicStatusCache(() => now);
  const load = async () => { reads++; return signal(pending, cursorFor(latest)); };
  const first = await (await cache.get(load)).json();
  pending = false; now = 10000;
  const cached = await cache.get(load);
  assert.equal(cached.headers.get('Cache-Control'), 'public, max-age=20, must-revalidate');
  assert.deepEqual(await cached.json(), first);
  now = 30000;
  const published = await (await cache.get(load)).json();
  assert.equal(published.pending, false); assert.equal(published.cursor, first.cursor);
  pending = true; latest = 'response-b'; now = 60000;
  const inserted = await (await cache.get(load)).json();
  assert.equal(inserted.pending, true); assert.notEqual(inserted.cursor, first.cursor);
  assert.equal(reads, 3);
});

test('expired success is not served on D1 failure; failures cool down and recover', async () => {
  let now = 0, fail = false, reads = 0;
  const cache = new PublicStatusCache(() => now);
  const load = async () => { reads++; if (fail) throw Error('private provider details'); return signal(); };
  assert.equal((await cache.get(load)).status, 200);
  fail = true; now = 30000;
  const errors = await Promise.all(Array.from({length: 100}, () => cache.get(load)));
  for (const response of errors) {
    assert.equal(response.status, 503); assert.equal(response.headers.get('Cache-Control'), 'no-store');
    assert.deepEqual(await response.json(), {error:'storage_unavailable'});
  }
  assert.equal(reads, 2);
  fail = false; now = 32999; assert.equal((await cache.get(load)).status, 503);
  now = 33000; assert.equal((await cache.get(load)).status, 200);
  assert.equal(reads, 3);
});

test('public D1 failure, malformed snapshots and missing binding return safe 503', async () => {
  const failed = new StatusDB(); failed.fail = true;
  for (const env of [{DB: failed}, {DB: new StatusDB(1, null)}, {DB: new StatusDB(2, 'response-a')}, {}]) {
    const response = await handleRequest(request(), env);
    assert.equal(response.status, 503);
    assert.equal(response.headers.get('Cache-Control'), 'no-store');
    assert.deepEqual(await response.json(), {error:'storage_unavailable'});
  }
});

test('clock moving backward invalidates success and binding caches are separate', async () => {
  let now = 1000, reads = 0;
  const cache = new PublicStatusCache(() => now);
  const load = async () => { reads++; return signal(); };
  await cache.get(load); now = 999; await cache.get(load);
  assert.equal(reads, 2);
  const a = new StatusDB(), b = new StatusDB(1, 'response-b');
  assert.deepEqual(await (await handleRequest(request(), {DB: a})).json(), {pending:false,cursor:null});
  assert.deepEqual(await (await handleRequest(request(), {DB: b})).json(), {pending:true,cursor:cursorFor('response-b')});
  assert.equal(a.queries.length, 1); assert.equal(b.queries.length, 1);
});

test('public cache does not weaken or cache the authenticated internal and admin routes', async () => {
  const db = new StatusDB();
  const env = {DB: db, MOONBACK_INTAKE_SECRET:'synthetic-intake-auth', MOONBACK_RESPONSE_ADMIN_SECRET:'synthetic-admin-auth'};
  await handleRequest(request(), env);
  for (const path of ['/v1/responses/status', '/v1/admin/responses']) {
    const denied = await handleRequest(request(path), env);
    assert.equal(denied.status, 404);
  }
  assert.equal(db.queries.length, 1);
  db.pending = 1; db.latest = 'response-new';
  const internal = await handleRequest(request('/v1/responses/status', {headers:{Authorization:'Bearer synthetic-intake-auth'}}), env);
  assert.equal(internal.status, 200); assert.equal(internal.headers.get('Cache-Control'), 'no-store');
  assert.deepEqual(await internal.json(), {pending:true,cursor:cursorFor('response-new')});
  assert.equal(db.queries.length, 2);
});

test('public reads never log, fetch another URL or execute mutations', async () => {
  const originalFetch = globalThis.fetch, originalLog = console.log;
  globalThis.fetch = () => assert.fail('unexpected fetch');
  console.log = () => assert.fail('unexpected log');
  try {
    const db = new StatusDB();
    const response = await handleRequest(request(), {DB: db});
    assert.equal(response.status, 200); assert.equal(db.queries.length, 1);
    assert.ok(db.queries.every(sql => sql.startsWith('SELECT ')));
  } finally { globalThis.fetch = originalFetch; console.log = originalLog; }
});
