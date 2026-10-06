const MAX_BODY_BYTES = 8 * 1024;
const MAX_PENDING_TOTAL = 1000;
const MAX_PENDING_PER_JOURNAL = 100;
const FIELDS = new Set([
  "id", "journal_id", "received_at", "agent_name", "response", "source_url",
]);
const ID_PATTERN = /^[a-z0-9][a-z0-9_-]{0,79}$/;
const UTC_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/;
const CONTROL = /\p{C}/u;

function json(item, status) {
  return new Response(JSON.stringify(item), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

function readJsonString(text, start) {
  if (text[start] !== '"') throw new Error("invalid_json");
  let index = start + 1;
  while (index < text.length) {
    if (text[index] === '"') {
      const raw = text.slice(start, index + 1);
      return [JSON.parse(raw), index + 1];
    }
    if (text[index] === "\\") {
      index += 1;
      if (index >= text.length) throw new Error("invalid_json");
      if (text[index] === "u") {
        if (!/^[0-9a-fA-F]{4}$/.test(text.slice(index + 1, index + 5))) {
          throw new Error("invalid_json");
        }
        index += 4;
      } else if (!'"\\/bfnrt'.includes(text[index])) {
        throw new Error("invalid_json");
      }
    } else if (text.charCodeAt(index) < 0x20) {
      throw new Error("invalid_json");
    }
    index += 1;
  }
  throw new Error("invalid_json");
}

function whitespace(text, start) {
  let index = start;
  while (index < text.length && /[ \t\r\n]/u.test(text[index])) index += 1;
  return index;
}

export function parseStrictStringObject(text) {
  let index = whitespace(text, 0);
  if (text[index] !== "{") throw new Error("invalid_json");
  index = whitespace(text, index + 1);
  const result = {};
  const seen = new Set();
  if (text[index] === "}") {
    index = whitespace(text, index + 1);
    if (index !== text.length) throw new Error("invalid_json");
    return result;
  }
  while (index < text.length) {
    let key;
    [key, index] = readJsonString(text, index);
    if (seen.has(key)) throw new Error("duplicate_key");
    seen.add(key);
    index = whitespace(text, index);
    if (text[index] !== ":") throw new Error("invalid_json");
    index = whitespace(text, index + 1);
    let value;
    [value, index] = readJsonString(text, index);
    result[key] = value;
    index = whitespace(text, index);
    if (text[index] === "}") {
      index = whitespace(text, index + 1);
      if (index !== text.length) throw new Error("invalid_json");
      return result;
    }
    if (text[index] !== ",") throw new Error("invalid_json");
    index = whitespace(text, index + 1);
  }
  throw new Error("invalid_json");
}

function validPlainText(value, maximum, multiline = false) {
  if (typeof value !== "string" || !value.trim()
      || Array.from(value).length > maximum) {
    return false;
  }
  const checked = multiline ? value.replaceAll("\n", "") : value;
  return !CONTROL.test(checked);
}

function validUtcTimestamp(value) {
  const match = UTC_TIMESTAMP.exec(value);
  if (!match) return false;
  const [datePart, timePart] = value.slice(0, 19).split("T");
  const [year, month, day] = datePart.split("-").map(Number);
  const [hour, minute, second] = timePart.split(":").map(Number);
  const parsed = new Date(Date.UTC(year, month - 1, day, hour, minute, second));
  return parsed.getUTCFullYear() === year
    && parsed.getUTCMonth() === month - 1
    && parsed.getUTCDate() === day
    && parsed.getUTCHours() === hour
    && parsed.getUTCMinutes() === minute
    && parsed.getUTCSeconds() === second;
}

function validSourceUrl(value) {
  if (typeof value !== "string") return false;
  if (value === "") return true;
  if (value.length > 2048 || /\s/u.test(value) || CONTROL.test(value)) return false;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:"
      && Boolean(parsed.hostname)
      && !parsed.username
      && !parsed.password
      && !parsed.search
      && !parsed.hash
      && (parsed.port === "" || parsed.port === "443");
  } catch {
    return false;
  }
}

export function validateDraft(item) {
  if (item === null || typeof item !== "object" || Array.isArray(item)) return false;
  const keys = Object.keys(item);
  if (keys.length !== FIELDS.size || keys.some((key) => !FIELDS.has(key))) return false;
  if (!ID_PATTERN.test(item.id) || !ID_PATTERN.test(item.journal_id)) return false;
  if (!validUtcTimestamp(item.received_at)) return false;
  return validPlainText(item.agent_name, 80)
    && validPlainText(item.response, 2000, true)
    && validSourceUrl(item.source_url);
}

async function digest(value) {
  return new Uint8Array(await crypto.subtle.digest(
    "SHA-256", new TextEncoder().encode(value),
  ));
}

export async function safeSecretEqual(provided, expected) {
  if (typeof provided !== "string" || typeof expected !== "string"
      || !provided || !expected || provided.length > 512 || expected.length > 512) {
    return false;
  }
  // Hashes have equal length, so the comparison loop does not reveal the
  // first differing secret character through early-return timing.
  const [left, right] = await Promise.all([digest(provided), digest(expected)]);
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left[index] ^ right[index];
  }
  return difference === 0;
}

async function authorized(request, env) {
  const header = request.headers.get("Authorization") || "";
  if (!header.startsWith("Bearer ")) return false;
  return safeSecretEqual(header.slice(7), env.MOONBACK_INTAKE_SECRET);
}

async function readLimitedBody(request) {
  if (!request.body) throw new Error("invalid_json");
  const reader = request.body.getReader();
  const chunks = [];
  let length = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    length += value.byteLength;
    if (length > MAX_BODY_BYTES) {
      await reader.cancel();
      throw new Error("payload_too_large");
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

function isDuplicateIdError(error) {
  return error instanceof Error
    && /UNIQUE constraint failed:\s*responses\.id/i.test(error.message);
}

export async function handleRequest(request, env) {
  const url = new URL(request.url);
  if (request.method !== "POST" || url.pathname !== "/v1/responses"
      || url.search || url.hash) {
    return json({ error: "not_found" }, 404);
  }
  if (!(await authorized(request, env))) {
    return json({ error: "not_found" }, 404);
  }
  const contentType = (request.headers.get("Content-Type") || "")
    .split(";", 1)[0].trim().toLowerCase();
  if (contentType !== "application/json") {
    return json({ error: "unsupported_media_type" }, 415);
  }
  const lengthHeader = request.headers.get("Content-Length") || "";
  if (!/^[0-9]+$/.test(lengthHeader)) {
    return json({ error: "content_length_required" }, 411);
  }
  const declaredLength = Number(lengthHeader);
  if (declaredLength > MAX_BODY_BYTES) return json({ error: "payload_too_large" }, 413);
  if (declaredLength === 0) return json({ error: "invalid_json" }, 400);

  let bytes;
  try {
    bytes = await readLimitedBody(request);
  } catch (error) {
    if (error instanceof Error && error.message === "payload_too_large") {
      return json({ error: "payload_too_large" }, 413);
    }
    return json({ error: "invalid_json" }, 400);
  }
  if (bytes.length > MAX_BODY_BYTES) return json({ error: "payload_too_large" }, 413);
  if (bytes.length !== declaredLength) return json({ error: "invalid_body_framing" }, 400);

  let item;
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    item = parseStrictStringObject(text);
  } catch {
    return json({ error: "invalid_response" }, 400);
  }
  if (!validateDraft(item)) return json({ error: "invalid_response" }, 400);

  try {
    const total = await env.DB.prepare(
      "SELECT COUNT(*) AS count FROM responses WHERE status = 'pending'",
    ).first();
    const journal = await env.DB.prepare(
      "SELECT COUNT(*) AS count FROM responses WHERE status = 'pending' AND journal_id = ?1",
    ).bind(item.journal_id).first();
    const totalCount = Number(total?.count);
    const journalCount = Number(journal?.count);
    if (!Number.isSafeInteger(totalCount) || totalCount < 0
        || !Number.isSafeInteger(journalCount) || journalCount < 0) {
      return json({ error: "storage_unavailable" }, 503);
    }
    if (totalCount >= MAX_PENDING_TOTAL
        || journalCount >= MAX_PENDING_PER_JOURNAL) {
      return json({ error: "capacity_reached" }, 429);
    }

    const result = await env.DB.prepare(`INSERT INTO responses (
      id, journal_id, received_at,
      agent_name, response, source_url, status
    ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'pending')`)
      .bind(
        item.id, item.journal_id, item.received_at,
        item.agent_name, item.response, item.source_url || null,
      ).run();
    if (!result?.success || Number(result?.meta?.changes) !== 1) {
      return json({ error: "storage_unavailable" }, 503);
    }
  } catch (error) {
    if (isDuplicateIdError(error)) return json({ error: "duplicate_id" }, 409);
    return json({ error: "storage_unavailable" }, 503);
  }
  return json({ status: "stored", receipt_id: item.id }, 201);
}

export default {
  fetch(request, env) {
    return handleRequest(request, env);
  },
};
