import assert from "node:assert/strict";
import test from "node:test";

import { handleRequest, safeSecretEqual } from "../src/index.js";

const SECRET = "test-only-shared-secret";
const valid = {
  id: "response-20260930-0123456789abcdef0123456789abcdef",
  journal_id: "journal-2026-09-28-test",
  received_at: "2026-09-30T01:02:03Z",
  agent_name: "Example Agent",
  response: "A short response.",
  source_url: "https://example.org/agent",
};

class FakeDB {
  constructor({ total = 0, journal = 0, duplicate = false, failure = false } = {}) {
    this.total = total;
    this.journal = journal;
    this.duplicate = duplicate;
    this.failure = failure;
    this.insert = null;
  }

  prepare(sql) {
    const database = this;
    let bindings = [];
    return {
      bind(...values) {
        bindings = values;
        return this;
      },
      async first() {
        return { count: sql.includes("journal_id") ? database.journal : database.total };
      },
      async run() {
        if (database.duplicate) {
          throw new Error("UNIQUE constraint failed: responses.id");
        }
        if (database.failure) throw new Error("private provider details");
        database.insert = { sql, bindings };
        return { success: true, meta: { changes: 1 } };
      },
    };
  }
}

function request(body = JSON.stringify(valid), changes = {}) {
  const bytes = new TextEncoder().encode(body);
  const headers = new Headers({
    Authorization: `Bearer ${SECRET}`,
    "Content-Type": "application/json",
    "Content-Length": String(bytes.length),
    ...(changes.headers || {}),
  });
  const method = changes.method || "POST";
  const options = { method, headers };
  if (method !== "GET" && method !== "HEAD") options.body = body;
  return new Request(changes.url || "https://worker.example/v1/responses", options);
}

async function run(body, changes = {}, database = new FakeDB()) {
  const response = await handleRequest(request(body, changes), {
    DB: database,
    MOONBACK_INTAKE_SECRET: SECRET,
  });
  return { response, payload: await response.json(), database };
}

test("valid request uses a bound pending INSERT and returns a fixed receipt", async () => {
  const result = await run(JSON.stringify(valid));
  assert.equal(result.response.status, 201);
  assert.deepEqual(result.payload, { status: "stored", receipt_id: valid.id });
  assert.match(result.database.insert.sql, /VALUES \(\?1, \?2, \?3, \?4, \?5, \?6, 'pending'\)/);
  assert.deepEqual(result.database.insert.bindings, [
    valid.id, valid.journal_id, valid.received_at,
    valid.agent_name, valid.response, valid.source_url,
  ]);
  assert.doesNotMatch(result.database.insert.sql, new RegExp(valid.agent_name));
});

test("authorization is required and compared without early character return", async () => {
  assert.equal(await safeSecretEqual(SECRET, SECRET), true);
  assert.equal(await safeSecretEqual("wrong-secret", SECRET), false);
  const result = await run(JSON.stringify(valid), {
    headers: { Authorization: "Bearer wrong-secret" },
  });
  assert.equal(result.response.status, 404);
  assert.equal(result.database.insert, null);
});

test("method, path, content type, and declared length are strict", async () => {
  const cases = [
    [{ method: "GET" }, 404],
    [{ url: "https://worker.example/v1/responses/" }, 404],
    [{ headers: { "Content-Type": "text/plain" } }, 415],
    [{ headers: { "Content-Length": "" } }, 411],
  ];
  for (const [changes, expected] of cases) {
    const result = await run(JSON.stringify(valid), changes);
    assert.equal(result.response.status, expected);
    assert.equal(result.database.insert, null);
  }
});

test("actual body over 8 KiB is rejected before parsing", async () => {
  const result = await run("x".repeat(8193));
  assert.equal(result.response.status, 413);
  assert.equal(result.database.insert, null);
});

test("duplicate and unknown JSON keys are rejected", async () => {
  const duplicate = JSON.stringify(valid).replace(
    '"agent_name":"Example Agent"',
    '"agent_name":"Example Agent","agent_name":"Second"',
  );
  for (const body of [duplicate, JSON.stringify({ ...valid, status: "pending" })]) {
    const result = await run(body);
    assert.equal(result.response.status, 400);
    assert.equal(result.database.insert, null);
  }
});

test("HTTP source URL and invalid draft metadata are rejected", async () => {
  for (const item of [
    { ...valid, source_url: "http://example.org/" },
    { ...valid, journal_id: "../journal" },
    { ...valid, response: "bad\u0000text" },
  ]) {
    const result = await run(JSON.stringify(item));
    assert.equal(result.response.status, 400);
    assert.equal(result.database.insert, null);
  }
});

test("total and per-Journal pending limits return 429", async () => {
  for (const database of [new FakeDB({ total: 1000 }), new FakeDB({ journal: 100 })]) {
    const result = await run(JSON.stringify(valid), {}, database);
    assert.equal(result.response.status, 429);
    assert.equal(result.database.insert, null);
  }
});

test("duplicate id returns 409 and other D1 failure returns 503", async () => {
  let result = await run(JSON.stringify(valid), {}, new FakeDB({ duplicate: true }));
  assert.equal(result.response.status, 409);
  result = await run(JSON.stringify(valid), {}, new FakeDB({ failure: true }));
  assert.equal(result.response.status, 503);
  assert.doesNotMatch(JSON.stringify(result.payload), /private provider details/);
});

test("handler emits no custom logs or submitted values", async () => {
  const original = { log: console.log, warn: console.warn, error: console.error };
  const calls = [];
  console.log = (...args) => calls.push(args);
  console.warn = (...args) => calls.push(args);
  console.error = (...args) => calls.push(args);
  try {
    const result = await run(JSON.stringify(valid));
    assert.equal(result.response.status, 201);
  } finally {
    Object.assign(console, original);
  }
  assert.deepEqual(calls, []);
});
