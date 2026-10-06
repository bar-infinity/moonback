from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime, timezone
import ipaddress, json, re, os
from content import FileStore, render_page, validate
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
RESPONSE_RATE_LIMITER = LocalResponseRateLimiter()


class DuplicateJSONKey(ValueError):
    pass


def strict_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJSONKey()
        result[key] = value
    return result


def response_store_for_request(client_address):
    """Select one explicit intake backend; never fall back between stores."""
    backend = os.environ.get('MOONBACK_RESPONSE_BACKEND')
    if backend == 'cloudflare':
        worker_url = os.environ.get('MOONBACK_RESPONSE_WORKER_URL')
        secret = os.environ.get('MOONBACK_RESPONSE_WORKER_SECRET')
        if not worker_url or not secret:
            return None, None
        try:
            return 'cloudflare', CloudflareResponseStore(worker_url, secret)
        except ValueError:
            return None, None
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
        "recent": rows[-20:][::-1]
    }

class Handler(SimpleHTTPRequestHandler):
    PUBLIC_FILES = {
        "/": "index.html", "/index.html": "index.html",
        "/observe": "observe.html", "/observe.html": "observe.html",
        "/agent.json": "agent.json", "/llms.txt": "llms.txt",
        "/robots.txt": "robots.txt", "/sitemap.xml": "sitemap.xml",
        **{f"/signal/{i:03d}{suffix}": f"signal/{i:03d}.json"
           for i in range(1, 6) for suffix in ("", ".json")}
    }

    def translate_path(self, path):
        return str(ROOT / self.PUBLIC_FILES[urlparse(path).path])

    def do_HEAD(self):
        self.serve(head=True)

    def do_GET(self):
        self.serve(head=False)

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

    def serve(self, head=False):
        path = urlparse(self.path).path
        if path == "/stats":
            payload = json.dumps(read_stats()).encode("utf-8")
            self.send_payload(payload, "application/json; charset=utf-8", head)
            return

        ua = self.headers.get("User-Agent", "")
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "path": path,
            "ua": ua[:180],
            "agent_like": classify_agent(ua)
        }
        LOG.parent.mkdir(exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # An allowlist prevents drafts, logs, backups and source files leaking.
        if path not in self.PUBLIC_FILES and path not in ("/archive", "/journal"):
            self.send_error(404)
            return

        if path in ("/archive", "/journal"):
            try:
                payload = render_page(path[1:])
            except (ValueError, OSError, TypeError):
                self.send_error(503, "Published content unavailable")
                return
            self.send_payload(payload, "text/html; charset=utf-8", head)
        elif head:
            super().do_HEAD()
        else:
            super().do_GET()

    def send_payload(self, payload, content_type, head=False):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head:
            self.wfile.write(payload)

    def send_json(self, item, *, status):
        payload = json.dumps(item, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(payload)

if __name__ == "__main__":
    os.chdir(ROOT)
    port = int(os.environ.get("PORT", "8000"))
    print(f"Moonback running at http://127.0.0.1:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
