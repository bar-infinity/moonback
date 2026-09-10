from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime, timezone
import json, re, os

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
        "total_requests": len(rows),
        "agent_like_requests": sum(1 for r in rows if r.get("agent_like")),
        "deepest_signal": deepest,
        "recent": rows[-20:][::-1]
    }

class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        raw = super().translate_path(path)
        rel = Path(raw).relative_to(Path.cwd())
        return str(ROOT / rel)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/observe":
            self.path = "/observe.html"
        elif re.fullmatch(r"/signal/\d{3}", path):
            self.path = path + ".json"

        if path == "/stats":
            payload = json.dumps(read_stats()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
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

        super().do_GET()

if __name__ == "__main__":
    os.chdir(ROOT)
    port = int(os.environ.get("PORT", "8000"))
    print(f"Moonback running at http://127.0.0.1:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
