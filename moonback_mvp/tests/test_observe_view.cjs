const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const vm = require('node:vm');

const html = readFileSync(join(__dirname, '../observe.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

async function render(recent) {
  const node = () => Object.defineProperty({ textContent: '' }, 'innerHTML', {
    set() { throw new Error('HTML rendering prohibited'); },
  });
  const context = {
    started: node(), total: node(), agents: node(), deepest: node(), recent: node(),
    fetch: async path => {
      assert.equal(path, '/stats');
      return { json: async () => ({ recent, total_requests: recent.length,
        agent_like_requests: 1, deepest_signal: '005' }) };
    },
    setInterval: (_, ms) => assert.equal(ms, 5000),
  };
  vm.runInNewContext(script, context);
  await new Promise(resolve => setImmediate(resolve));
  return context;
}

test('Observe shows method/status and preserves counts and agent uncertainty', async () => {
  const view = await render([{ time: '2026-11-05T14:20:30+00:00', path: '/tools/',
    ua: 'ExampleClient', agent_like: true, method: 'HEAD', status: 200,
    response_completed: true }]);
  assert.match(view.recent.textContent, /23:20:30 JST  HEAD 200 \/tools\/  \[agent\?\]/);
  assert.equal(view.total.textContent, 1);
  assert.equal(view.deepest.textContent, '005');
});

test('Observe leaves old and invalid method/status unknown', async () => {
  const view = await render([
    { path: '/old', ua: '', agent_like: false },
    { path: '/unknown', ua: '', method: null, status: '200' },
  ]);
  assert.match(view.recent.textContent, /- - \/old/);
  assert.match(view.recent.textContent, /- - \/unknown/);
  assert.doesNotMatch(view.recent.textContent, /200/);
});

test('Observe marks interrupted sends and treats external text literally', async () => {
  const view = await render([{ path: '/missing', ua: '<img src=x onerror=alert(1)>',
    method: 'GET', status: 200, response_completed: false }]);
  assert.match(view.recent.textContent, /GET 200/);
  assert.match(view.recent.textContent, /\[send incomplete\]/);
  assert.ok(view.recent.textContent.includes('<img src=x onerror=alert(1)>'));
});
