from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime, timezone
import json, re, os
from content import render_page

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "data" / "requests.jsonl"
AGENT_HINTS = (
    "bot", "crawler", "spider", "gpt", "chatgpt", "openai",
    "claude", "anthropic", "gemini", "google-extended", "perplexity",
    "bytespider", "ccbot"
)

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

if __name__ == "__main__":
    os.chdir(ROOT)
    port = int(os.environ.get("PORT", "8000"))
    print(f"Moonback running at http://127.0.0.1:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
