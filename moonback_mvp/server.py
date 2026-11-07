from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote_to_bytes, urlparse
from datetime import datetime, timezone
import ipaddress, json, re, os, threading, time
from content import (FileStore, render_page, render_sitemap, validate,
                     CONTENT_ID, PUBLIC_ORIGIN, PublishedItemNotFound)
import tools_web
from observation_archive import configured_archive, new_event_fields
from response import ResponseValidationError, build_draft_response
from response_store import (
    CloudflareResponseStore, CloudflareStoreError, FileResponseStore,
    LocalResponseRateLimiter,
)

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "data" / "requests.jsonl"
AGENT_HINTS = (
    "bot", "crawler", "spider", "gpt", "chatgpt", "openai",
    "claude", "anthropic", "gemini", "google-extended", "perplexity",
    "bytespider", "ccbot"
)
MAX_RESPONSE_BODY = 8 * 1024
MAX_RESPONSE_DRAFTS = 1000
RESPONSE_STATUS_CACHE_SECONDS = 30
RESPONSE_RATE_LIMITER = LocalResponseRateLimiter()
OBSERVATION_LOG_LOCK = threading.Lock()
OBSERVATION_ARCHIVE = None  # Configured once by main(); imports/tests never activate it.
TRACKING_KEYS = frozenset((
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term',
    'gclid', 'fbclid',
))
MAX_TRACKING_QUERY = 2048
MAX_TRACKING_VALUE = 512
TRACKING_VALUE = re.compile(r'(?:[A-Za-z0-9._~+\-]|%(?![01][0-9a-fA-F]|7[fF])[0-9a-fA-F]{2})+')


def public_tracking_target(raw_target, path):
    """Match the literal path and discard tracking values after validation."""
    raw_path, separator, query = raw_target.partition('?')
    if raw_path != path:
        return False
    if not separator:
        return True
    if not query or len(query) > MAX_TRACKING_QUERY:
        return False
    fields = query.split('&')
    if len(fields) > len(TRACKING_KEYS):
        return False
    seen = set()
    for field in fields:
        key, equals, value = field.partition('=')
        if (key not in TRACKING_KEYS or key in seen or not equals or
                len(value) > MAX_TRACKING_VALUE or not TRACKING_VALUE.fullmatch(value)):
            return False
        try:
            unquote_to_bytes(value).decode('utf-8', errors='strict')
        except UnicodeDecodeError:
            return False
        seen.add(key)
    return True


class DuplicateJSONKey(ValueError):
    pass


def strict_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJSONKey()
        result[key] = value
    return result


class ResponseStatusCache:
    """Short process-local cache with no stale-on-error fallback."""

    def __init__(self, ttl=RESPONSE_STATUS_CACHE_SECONDS):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._value = None
        self._expires_at = 0.0

    def get(self, loader):
        with self._lock:
            now = time.monotonic()
            if self._value is not None and now < self._expires_at:
                return dict(self._value)
            value = loader()
            self._value = dict(value)
            self._expires_at = time.monotonic() + self.ttl
            return dict(self._value)


RESPONSE_STATUS_CACHE = ResponseStatusCache()


def configured_cloudflare_response_store():
    if os.environ.get('MOONBACK_RESPONSE_BACKEND') != 'cloudflare':
        return None
    worker_url = os.environ.get('MOONBACK_RESPONSE_WORKER_URL')
    secret = os.environ.get('MOONBACK_RESPONSE_WORKER_SECRET')
    if not worker_url or not secret:
        return None
    try:
        return CloudflareResponseStore(worker_url, secret)
    except ValueError:
        return None


def response_store_for_request(client_address):
    """Select one explicit intake backend; never fall back between stores."""
    backend = os.environ.get('MOONBACK_RESPONSE_BACKEND')
    if backend == 'cloudflare':
        store = configured_cloudflare_response_store()
        return ('cloudflare', store) if store is not None else (None, None)
    if backend not in (None, ''):
        return None, None
    if os.environ.get('MOONBACK_ENABLE_RESPONSE_POST') != '1':
        return None, None
    try:
        if not ipaddress.ip_address(client_address).is_loopback:
            return None, None
    except (ValueError, TypeError):
        return None, None
    return 'file', FileResponseStore()

def classify_agent(ua: str) -> bool:
    u = (ua or "").lower()
    return any(h in u for h in AGENT_HINTS)

def read_stats():
    rows = []
    if LOG.exists():
        for line in LOG.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    deepest = None
    depth = -1
    for row in rows:
        m = re.fullmatch(r"/signal/(\d{3})(?:\.json)?", row.get("path",""))
        if m and int(m.group(1)) > depth:
            depth = int(m.group(1))
            deepest = m.group(1)
    return {
        "observation_started": rows[0].get("time") if rows else None,
        "total_requests": len(rows),
        "agent_like_requests": sum(1 for r in rows if r.get("agent_like")),
        "deepest_signal": deepest,
        # Delivery identifiers stay internal; preserve the existing public stats fields.
        "recent": [{k: v for k, v in row.items()
                    if k not in ("event_id", "schema_version")}
                   for row in rows[-20:][::-1]]
    }

class Handler(SimpleHTTPRequestHandler):
    PUBLIC_FILES = {
        "/": "index.html", "/index.html": "index.html",
        "/observe": "observe.html", "/observe.html": "observe.html",
        "/agent.json": "agent.json", "/llms.txt": "llms.txt",
        "/robots.txt": "robots.txt", "/sitemap.xml": "sitemap.xml",
        "/google1c6ec0bf65f9cfd3.html": "google1c6ec0bf65f9cfd3.html",
        **{f"/signal/{i:03d}{suffix}": f"signal/{i:03d}.json"
           for i in range(1, 6) for suffix in ("", ".json")}
    }

    def translate_path(self, path):
        return str(ROOT / self.PUBLIC_FILES[urlparse(path).path])

    def do_HEAD(self):
        self.serve_observed(head=True)

    def do_GET(self):
        self.serve_observed(head=False)

    def send_response_only(self, code, message=None):
        super().send_response_only(code, message)
        if getattr(self, '_observation_active', False) and int(code) >= 200:
            self._observation_pending_status = int(code)

    def end_headers(self):
        super().end_headers()
        if getattr(self, '_observation_active', False):
            # A buffered status is not evidence that its headers were sent.
            self._observation_status = self._observation_pending_status

    def serve_observed(self, *, head):
        path = urlparse(self.path).path
        # Preserve the existing scope, including exclusion of status polling.
        if path in ('/stats', '/api/responses/status'):
            self.serve(head=head)
            return
        self._observation_active = True
        self._observation_pending_status = None
        self._observation_status = None
        completed = False
        try:
            self.serve(head=head)
            self.wfile.flush()
            completed = self._observation_status is not None
        finally:
            entry = {
                **(new_event_fields() if OBSERVATION_ARCHIVE is not None else {}),
                'time': datetime.now(timezone.utc).isoformat(),
                'path': path,
                'method': self.command,
                'status': self._observation_status,
                # Local write completion does not prove client receipt.
                'response_completed': completed,
                'ua': self.headers.get('User-Agent', '')[:180],
                'agent_like': classify_agent(self.headers.get('User-Agent', '')),
            }
            self._observation_active = False
            try:
                with OBSERVATION_LOG_LOCK:
                    LOG.parent.mkdir(exist_ok=True)
                    with LOG.open('a', encoding='utf-8') as stream:
                        stream.write(json.dumps(entry, ensure_ascii=False) + '\n')
            except (OSError, UnicodeError, ValueError):
                # Observation failure must never replace a public response or
                # expose request data through diagnostics.
                pass
            # Never hold OBSERVATION_LOG_LOCK during queueing/network activity.
            if OBSERVATION_ARCHIVE is not None:
                try:
                    OBSERVATION_ARCHIVE.submit(entry)
                except Exception:
                    pass  # Archive failures cannot replace the public response.

    def log_request(self, code='-', size='-'):
        # The default HTTP access log includes the raw request line. Drop the
        # query there too; the Observe JSONL already records only the path.
        parts = self.requestline.split()
        if len(parts) >= 2 and '?' in parts[1]:
            parts[1] = parts[1].partition('?')[0]
            self.log_message('"%s" %s %s', ' '.join(parts), getattr(code, 'value', code), size)
        else:
            super().log_request(code, size)

    def do_POST(self):
        self.receive_response()

    def do_OPTIONS(self):
        self.send_json({'error': 'method_not_allowed'}, status=405)

    def receive_response(self):
        self.close_connection = True
        try:
            backend, response_store = response_store_for_request(
                self.client_address[0])
        except (IndexError, TypeError):
            backend, response_store = None, None
        if response_store is None:
            self.send_json({'error': 'not_found'}, status=404)
            return
        parsed = urlparse(self.path)
        match = re.fullmatch(
            r'/api/journal/([a-z0-9][a-z0-9_-]{0,79})/responses',
            parsed.path,
        )
        if match is None or parsed.query or parsed.fragment:
            self.send_json({'error': 'not_found'}, status=404)
            return
        journal_id = match.group(1)
        journal_store = FileStore()
        journal_path = journal_store.directory('published', 'journal') / (journal_id + '.json')
        try:
            journal_item = validate(
                'journal', json.loads(journal_path.read_text(encoding='utf-8')))
        except FileNotFoundError:
            self.send_json({'error': 'journal_not_found'}, status=404)
            return
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self.send_json({'error': 'service_unavailable'}, status=503)
            return
        if journal_item['id'] != journal_id:
            self.send_json({'error': 'journal_not_found'}, status=404)
            return
        if self.headers.get_content_type() != 'application/json':
            self.send_json({'error': 'unsupported_media_type'}, status=415)
            return
        if self.headers.get('Transfer-Encoding') is not None:
            self.send_json({'error': 'invalid_body_framing'}, status=400)
            return
        if self.headers.get('Content-Encoding') not in (None, 'identity'):
            self.send_json({'error': 'unsupported_content_encoding'}, status=415)
            return
        lengths = self.headers.get_all('Content-Length', failobj=[])
        if len(lengths) != 1 or re.fullmatch(r'[0-9]+', lengths[0]) is None:
            self.send_json({'error': 'content_length_required'}, status=411)
            return
        length = int(lengths[0])
        if length > MAX_RESPONSE_BODY:
            self.send_json({'error': 'payload_too_large'}, status=413)
            return
        if length == 0:
            self.send_json({'error': 'invalid_json'}, status=400)
            return
        body = self.rfile.read(length)
        try:
            submitted = json.loads(
                body.decode('utf-8'), object_pairs_hook=strict_json_object)
            draft = build_draft_response(submitted, journal_id)
        except (UnicodeError, json.JSONDecodeError, DuplicateJSONKey,
                ResponseValidationError):
            self.send_json({'error': 'invalid_response'}, status=400)
            return
        source = self.client_address[0]
        if not RESPONSE_RATE_LIMITER.allow(source, journal_id):
            self.send_json({'error': 'rate_limited'}, status=429)
            return
        try:
            if (backend == 'file'
                    and len(response_store.read_drafts()) >= MAX_RESPONSE_DRAFTS):
                self.send_json({'error': 'rate_limited'}, status=429)
                return
            response_store.save_draft(draft)
        except CloudflareStoreError as exc:
            status = 429 if exc.category == 'rate_limited' else (
                502 if exc.category == 'bad_gateway' else 503)
            self.send_json({'error': exc.category}, status=status)
            return
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self.send_json({'error': 'service_unavailable'}, status=503)
            return
        self.send_json({
            'status': 'accepted_for_review',
            'receipt_id': draft['id'],
        }, status=202)

    def serve_response_status(self, *, head=False):
        store = configured_cloudflare_response_store()
        if store is None:
            self.send_json({'error': 'not_found'}, status=404, head=head)
            return
        try:
            payload = RESPONSE_STATUS_CACHE.get(store.get_status)
        except CloudflareStoreError as exc:
            status = 502 if exc.category == 'bad_gateway' else 503
            self.send_json({'error': exc.category}, status=status, head=head)
            return
        self.send_json(
            payload, status=200, head=head,
            cache_control='public, max-age=30, must-revalidate',
        )

    def serve(self, head=False):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == '/api/responses/status':
            if parsed.query or parsed.fragment:
                self.send_json({'error': 'not_found'}, status=404, head=head)
                return
            self.serve_response_status(head=head)
            return
        if path == "/stats":
            payload = json.dumps(read_stats()).encode("utf-8")
            self.send_payload(payload, "application/json; charset=utf-8", head)
            return

        # An allowlist prevents drafts, logs, backups and source files leaking.
        detail = re.fullmatch(r'/(archive|journal)/(' + CONTENT_ID + r')', path)
        raw_target = self.requestline.split()[1]
        if (path in tools_web.ROUTES or detail or path in ('/archive', '/journal')):
            if not public_tracking_target(raw_target, path):
                self.send_error(404)
                return
        if path == '/google1c6ec0bf65f9cfd3.html' and raw_target != path:
            self.send_error(404)
            return
        if path in tools_web.ROUTES:
            try:
                payload, mime, attachment = tools_web.serve_asset(path, PUBLIC_ORIGIN)
            except tools_web.DATASET_ERRORS:
                self.send_error(503, 'Tools dataset unavailable')
                return
            self.send_payload(payload, mime, head, attachment=attachment)
            return
        if path not in self.PUBLIC_FILES and path not in ("/archive", "/journal") and not detail:
            self.send_error(404)
            return

        if path == '/sitemap.xml':
            try:
                payload = render_sitemap()
            except (ValueError, OSError, TypeError):
                self.send_error(503, 'Published content unavailable')
                return
            self.send_payload(payload, 'application/xml; charset=utf-8', head)
        elif path in ("/archive", "/journal") or detail:
            try:
                payload = (render_page(detail[1], ident=detail[2]) if detail
                           else render_page(path[1:]))
            except PublishedItemNotFound:
                self.send_error(404)
                return
            except (ValueError, OSError, TypeError):
                self.send_error(503, "Published content unavailable")
                return
            self.send_payload(payload, "text/html; charset=utf-8", head)
        elif head:
            super().do_HEAD()
        else:
            super().do_GET()

    def send_payload(self, payload, content_type, head=False, *, attachment=None):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if attachment is not None:
            self.send_header('Content-Disposition', f'attachment; filename="{attachment}"')
        self.end_headers()
        if not head:
            self.wfile.write(payload)

    def send_json(self, item, *, status, head=False, cache_control='no-store'):
        payload = json.dumps(item, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', cache_control)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        if not head:
            self.wfile.write(payload)

if __name__ == "__main__":
    os.chdir(ROOT)
    port = int(os.environ.get("PORT", "8000"))
    print(f"Moonback running at http://127.0.0.1:{port}")
    OBSERVATION_ARCHIVE = configured_archive()
    if OBSERVATION_ARCHIVE is not None:
        OBSERVATION_ARCHIVE.start()
    try:
        ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    finally:
        if OBSERVATION_ARCHIVE is not None:
            OBSERVATION_ARCHIVE.close()
