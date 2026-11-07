// Standalone private observation service. No imports/bindings from response intake.
import {handleAdmin} from "./admin.js";
const FIELDS = ['event_id', 'schema_version', 'time', 'path', 'method', 'status',
  'response_completed', 'ua', 'agent_like'];
export const INSERT = `INSERT INTO observation_events
(event_id,schema_version,time,path,method,status,response_completed,ua,agent_like,payload_hash)
VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)
ON CONFLICT(event_id) DO NOTHING`;
export const LOOKUP = 'SELECT payload_hash FROM observation_events WHERE event_id = ?1';
const MAX_BYTES = 8192;

// Exact documented D1 errors only; never infer capacity from "quota", SQL text,
// SQLITE_FULL alone, nested causes, or an arbitrary provider message fragment.
// https://developers.cloudflare.com/d1/observability/debug-d1/#list-of-d1_errors
const CAPACITY_MESSAGES = new Set([
  'Exceeded maximum DB size.',
  "Your account has exceeded D1's maximum account storage limit, please contact Cloudflare to raise your limit",
  "Your account has exceeded D1's free tier daily row read limit. Upgrade to a paid plan or wait until tomorrow (midnight UTC) to continue. See https://developers.cloudflare.com/d1/platform/limits/ for more details.",
  "Your account has exceeded D1's free tier daily row write limit. Upgrade to a paid plan or wait until tomorrow (midnight UTC) to continue. See https://developers.cloudflare.com/d1/platform/limits/ for more details.",
]);

function knownCapacityError(error) {
  try {
    if (typeof error?.message !== 'string') return false;
    const message = error.message.startsWith('D1_ERROR: ')
      ? error.message.slice('D1_ERROR: '.length) : error.message;
    return CAPACITY_MESSAGES.has(message);
  } catch { return false; }
}

function reply(error, status) {
  return new Response(JSON.stringify(error), {status, headers: {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
  }});
}
async function digest(value) {
  return new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)));
}
async function authenticate(header, secret) {
  if (typeof secret !== 'string' || !/^[\x21-\x7e]+$/.test(secret)
      || typeof header !== 'string' || header.length > 4096) return false;
  // Hash to fixed length and compare all bytes; never log credentials.
  const [a, b] = await Promise.all([digest(header), digest(`Bearer ${secret}`)]);
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

// This schema is flat and scalar-only. Tokenize each key/value before JSON.parse
// so duplicate (including escaped-equivalent) keys cannot be silently overwritten.
export function strictObject(text) {
  JSON.parse(text); // Enforce JSON grammar/whitespace before duplicate-key scanning.
  const string = '"(?:[^"\\\\\\x00-\\x1f]|\\\\(?:["\\\\/bfnrt]|u[0-9a-fA-F]{4}))*"';
  const scalar = `(?:${string}|true|false|null|-?(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)`;
  const pair = new RegExp(`\\s*(${string})\\s*:\\s*(${scalar})\\s*`, 'y');
  let offset = 0;
  while (/\s/.test(text[offset] || '') && offset < text.length) offset++;
  if (text[offset++] !== '{') throw new Error('invalid');
  const result = Object.create(null);
  while (true) {
    pair.lastIndex = offset;
    const match = pair.exec(text);
    if (!match) throw new Error('invalid');
    const key = JSON.parse(match[1]);
    if (Object.hasOwn(result, key)) throw new Error('invalid');
    result[key] = JSON.parse(match[2]);
    offset = pair.lastIndex;
    const end = text[offset++];
    if (end === '}') break;
    if (end !== ',') throw new Error('invalid');
  }
  if (text.slice(offset).trim()) throw new Error('invalid');
  return result;
}
function validTime(value) {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value)) return false;
  const iso = new Date(value);
  return Number.isFinite(iso.getTime()) && iso.toISOString().slice(0, 19) === value.slice(0, 19);
}
function secretLike(value) {
  // Defense in depth matching the credential families in journal_safety.py.
  const patterns = [
    /-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----/i,
    /\bsk-(?:[a-z]+-)?[a-z0-9_-]{20,}\b/i,
    /\b(?:gh[pousr]_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,})\b/i,
    /\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/i,
    /\beyJ[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\b/i,
    /\bauthorization\s*:\s*(?:bearer|basic)\s+[a-z0-9+/_.=-]{8,}/i,
    /\b(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|refresh[_ -]?token|client[_ -]?secret|password|passwd|credential|secret|token)["']?\s*[:=]\s*["']?[^\s"'<>;,]{4,}/i,
  ];
  let decoded = value;
  try { decoded = decodeURIComponent(value); } catch { /* check literal value */ }
  return patterns.some(p => p.test(value) || p.test(decoded));
}
export function validate(event) {
  if (Object.keys(event).length !== FIELDS.length || FIELDS.some(k => !Object.hasOwn(event, k))) return false;
  if (!/^obs-[0-9a-f]{32}$/.test(event.event_id) || typeof event.event_id !== 'string'
      || event.schema_version !== 1) return false;
  for (const [key, max] of [['time',32],['path',2048],['method',4],['ua',180]]) {
    const v = event[key];
    if (typeof v !== 'string' || Array.from(v).length > max || /\p{C}/u.test(v) || secretLike(v)) return false;
  }
  return validTime(event.time) && event.path.startsWith('/') && !/[?#]/.test(event.path)
    && ['GET','HEAD'].includes(event.method)
    && (event.status === null || (Number.isInteger(event.status) && event.status >= 200 && event.status <= 599))
    && typeof event.response_completed === 'boolean' && typeof event.agent_like === 'boolean';
}
async function readBody(request) {
  const reader = request.body?.getReader();
  if (!reader) throw new Error('invalid');
  const chunks = []; let size = 0;
  while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_BYTES) { await reader.cancel(); throw new Error('invalid'); }
    chunks.push(value);
  }
  if (size !== Number(request.headers.get('Content-Length'))) throw new Error('invalid');
  const bytes = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  return new TextDecoder('utf-8', {fatal:true}).decode(bytes);
}
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const readSecret = env.MOONBACK_OBSERVATION_READ_SECRET;
    const collision = typeof readSecret === 'string' && readSecret.length > 0
      && await authenticate('Bearer ' + readSecret, env.MOONBACK_OBSERVATION_INGEST_SECRET);
    const admin = await handleAdmin(request, env, authenticate, collision);
    if (admin !== null) return admin;
    if (collision || request.method !== 'POST' || url.pathname !== '/v1/observations' || url.search
        || !await authenticate(request.headers.get('Authorization'), env.MOONBACK_OBSERVATION_INGEST_SECRET)) {
      return reply({error:'not_found'},404);
    }
    const size = request.headers.get('Content-Length');
    if (request.headers.get('Content-Type')?.toLowerCase() !== 'application/json'
        || request.headers.has('Transfer-Encoding') || !/^[1-9][0-9]*$/.test(size || '')
        || Number(size) > MAX_BYTES) return reply({error:'invalid_request'},400);
    let event;
    try {
      event = strictObject(await readBody(request));
      if (!validate(event)) throw new Error('invalid');
    } catch { return reply({error:'invalid_request'},400); }
    try {
      const hash = Array.from(await digest(JSON.stringify(FIELDS.map(k => event[k]))),
        n => n.toString(16).padStart(2,'0')).join('');
      const values = FIELDS.map(k => typeof event[k] === 'boolean' ? Number(event[k]) : event[k]);
      const result = await env.OBS_DB.prepare(INSERT).bind(...values,hash).run();
      if (!result.success || !Number.isInteger(result.meta?.changes)) throw new Error('database');
      if (result.meta.changes === 0) {
        const existing = await env.OBS_DB.prepare(LOOKUP).bind(event.event_id).first();
        if (!existing) throw new Error('database');
        if (existing.payload_hash !== hash) return reply({error:'conflict'},409);
      }
      return reply({status:'stored',event_id:event.event_id},201);
    } catch (error) {
      // Never return/log internal errors. Unknown quota/storage failures stay503.
      if (knownCapacityError(error)) return reply({error:'capacity'},507);
      return reply({error:'storage_unavailable'},503);
    }
  },
};
