// Read-only management boundary. Never accept SQL or call external services.
const ID = /^[a-z0-9][a-z0-9_-]{0,79}$/;
const TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/;
const MAX_BYTES = 64 * 1024;
const SUMMARY_KEYS = ["id", "journal_id", "received_at"];
const DETAIL_KEYS = [...SUMMARY_KEYS, "agent_name", "response", "source_url"];

// Best-effort per-isolate guard, separate from intake. No IP or secrets stored.
export class AdminReadLimiter {
  constructor(limit = 60, windowMs = 60000) {
    this.limit = limit;
    this.windowMs = windowMs;
    this.events = [];
  }
  allow(now = Date.now()) {
    this.events = this.events.filter((instant) => instant > now - this.windowMs);
    if (this.events.length >= this.limit) return false;
    this.events.push(now);
    return true;
  }
}
const readLimiter = new AdminReadLimiter();

function reply(request, value, status = 200) {
  let text = JSON.stringify(value);
  if (new TextEncoder().encode(text).length > MAX_BYTES) {
    text = '{"error":"storage_unavailable"}';
    status = 503;
  }
  return new Response(request.method === "HEAD" ? null : text, {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

function validTimestamp(value) {
  if (typeof value !== "string") return false;
  const match = TIMESTAMP.exec(value);
  if (!match || match[0] !== value) return false;
  const parts = value.slice(0, 19).split(/[-T:]/).map(Number);
  const [year, month, day, hour, minute, second] = parts;
  if (year < 1) return false;
  // Round-trip every component: Date.parse() alone normalizes impossible dates.
  // setUTCFullYear also avoids Date.UTC's special interpretation of years 0–99.
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day);
  date.setUTCHours(hour, minute, second, 0);
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1
    && date.getUTCDate() === day && date.getUTCHours() === hour
    && date.getUTCMinutes() === minute && date.getUTCSeconds() === second;
}

function validSummary(row) {
  return row && typeof row.id === "string" && ID.test(row.id)
    && typeof row.journal_id === "string" && ID.test(row.journal_id)
    && validTimestamp(row.received_at);
}

function project(row, keys) {
  return Object.fromEntries(keys.map((key) => [key, row[key]]));
}

function validDetail(row) {
  if (!validSummary(row) || row.status !== "pending") return false;
  if (typeof row.agent_name !== "string" || !row.agent_name.trim()
      || Array.from(row.agent_name).length > 80) return false;
  if (typeof row.response !== "string" || !row.response.trim()
      || Array.from(row.response).length > 2000) return false;
  if (row.source_url === null || row.source_url === "") return true;
  if (typeof row.source_url !== "string" || row.source_url.length > 2048) return false;
  try {
    const url = new URL(row.source_url);
    return url.protocol === "https:" && Boolean(url.hostname)
      && !url.username && !url.password && !url.search && !url.hash
      && (url.port === "" || url.port === "443");
  } catch {
    return false;
  }
}

function encodeCursor(row) {
  return btoa(JSON.stringify([row.received_at, row.id]))
    .replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function decodeCursor(value) {
  if (!/^[A-Za-z0-9_-]{1,256}$/.test(value)) throw new Error("invalid_query");
  const parts = JSON.parse(atob(value.replaceAll("-", "+").replaceAll("_", "/")));
  if (!Array.isArray(parts) || parts.length !== 2
      || !validSummary({ received_at: parts[0], id: parts[1], journal_id: "cursor" })) {
    throw new Error("invalid_query");
  }
  if (encodeCursor({ received_at: parts[0], id: parts[1] }) !== value) {
    throw new Error("invalid_query");
  }
  return parts;
}

export async function handleAdminRequest(request, env, safeSecretEqual) {
  // Authenticate before parameter parsing and before any D1 access.
  const expected = env.MOONBACK_RESPONSE_ADMIN_SECRET;
  const header = request.headers.get("Authorization") || "";
  if (!expected || await safeSecretEqual(expected, env.MOONBACK_INTAKE_SECRET)
      || !header.startsWith("Bearer ")
      || !(await safeSecretEqual(header.slice(7), expected))) {
    return reply(request, { error: "not_found" }, 404);
  }
  if (request.method !== "GET" && request.method !== "HEAD") {
    return reply(request, { error: "not_found" }, 404);
  }
  const url = new URL(request.url);
  if (url.search.length > 1024) return reply(request, { error: "invalid_query" }, 400);
  const list = url.pathname === "/v1/admin/responses";
  const match = /^\/v1\/admin\/responses\/([a-z0-9][a-z0-9_-]{0,79})$/.exec(url.pathname);
  if ((!list && !match) || url.hash) return reply(request, { error: "not_found" }, 404);
  let limit = 20;
  let cursor = null;
  try {
    const seen = new Set();
    for (const [key, value] of url.searchParams) {
      if (!list || !["limit", "cursor"].includes(key) || seen.has(key)) {
        throw new Error("invalid_query");
      }
      seen.add(key);
      if (key === "limit") {
        if (!/^[1-9][0-9]?$/.test(value) || Number(value) > 50) throw new Error("invalid_query");
        limit = Number(value);
      } else cursor = decodeCursor(value);
    }
  } catch {
    return reply(request, { error: "invalid_query" }, 400);
  }
  if (!readLimiter.allow()) return reply(request, { error: "rate_limited" }, 429);
  try {
    if (!list) {
      const row = await env.DB.prepare(
        "SELECT id, journal_id, received_at, agent_name, response, source_url, status "
        + "FROM responses WHERE id = ?1 AND status = 'pending' LIMIT 1",
      ).bind(match[1]).first();
      if (row === null) return reply(request, { error: "not_found" }, 404);
      if (!validDetail(row)) return reply(request, { error: "storage_unavailable" }, 503);
      const item = project(row, DETAIL_KEYS);
      item.source_url = item.source_url || "";
      return reply(request, { item, status: "pending" });
    }
    let statement;
    if (cursor) {
      statement = env.DB.prepare(
        "SELECT id, journal_id, received_at FROM responses WHERE status = 'pending' "
        + "AND (received_at, id) > (?1, ?2) ORDER BY received_at ASC, id ASC LIMIT ?3",
      ).bind(cursor[0], cursor[1], limit + 1);
    } else {
      statement = env.DB.prepare(
        "SELECT id, journal_id, received_at FROM responses WHERE status = 'pending' "
        + "ORDER BY received_at ASC, id ASC LIMIT ?1",
      ).bind(limit + 1);
    }
    const result = await statement.all();
    if (!result?.success || !Array.isArray(result.results)
        || result.results.length > limit + 1 || !result.results.every(validSummary)) {
      return reply(request, { error: "storage_unavailable" }, 503);
    }
    const items = result.results.slice(0, limit).map((row) => project(row, SUMMARY_KEYS));
    const next_cursor = result.results.length > limit ? encodeCursor(items.at(-1)) : null;
    return reply(request, { items, next_cursor });
  } catch {
    return reply(request, { error: "storage_unavailable" }, 503);
  }
}
