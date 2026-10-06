"""Smoke test for the portal: every route answers, and the guards refuse.

Runs the real Handler in-process on a spare port, so it never touches a running
portal or its pid file. Read-only: it skips anything that starts a job, stops
the server, or calls a model (/api/llm is a live round trip).
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from congress_trades.config import CONFIG  # noqa: E402
from congress_trades.portal import server  # noqa: E402

# Every front-end file must be in git, not just on this disk: a *.html ignore rule
# once kept web/portal.html and web/page.html out of the repo, and a fresh clone
# could start neither the portal nor publish while every local check passed.
import subprocess  # noqa: E402
_root = Path(__file__).resolve().parent.parent
_tracked = subprocess.run(["git", "ls-files", "congress_trades/web"], cwd=_root,
                          capture_output=True, text=True)
if _tracked.returncode == 0:
    missing = sorted(f"congress_trades/web/{f.name}" for f in (_root / "congress_trades/web").iterdir()
                     if f"congress_trades/web/{f.name}" not in _tracked.stdout.split())
    assert not missing, f"front-end files not in git (check .gitignore): {missing}"

if not CONFIG.db_path.exists():
    print("skip: no database yet")
    sys.exit(0)

# Noob mode: every report and tab has a complete plain-language guide.
from congress_trades.portal.guide import GUIDES  # noqa: E402
from congress_trades.portal.jobs import REPORTS as _R  # noqa: E402
from congress_trades.portal.pages import TABS as _T  # noqa: E402
_need = {f"r/{k}" for k in _R} | set(_T)
assert _need <= set(GUIDES), f"no noob guide for {sorted(_need - set(GUIDES))}"
assert all(set(GUIDES[k]) == {"what", "why", "use", "careful"} and all(GUIDES[k].values())
           for k in _need), "a guide is missing a part"

httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{httpd.server_address[1]}"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=120) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def post(path, body=b"{}", headers=None):
    req = urllib.request.Request(BASE + path, data=body, method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


PAGES = ["/", "/jobs", "/page", "/feed.xml", "/r/compliance", "/r/flags", "/r/scorecard?limit=5",
         "/static/portal.js", "/static/portal.css", "/static/common.js"]
APIS = ["/status", "/api/stats", "/api/jobs", "/api/model", "/api/facets", "/api/names",
        "/api/watch", "/api/trades?limit=3&watched=1",
        "/api/trades?limit=3&sort=alpha", "/api/timeline?days=365", "/api/top",
        "/api/ticker?symbol=NVDA", "/api/member?name=Nancy%20Pelosi"]

for p in PAGES:
    code, _, body = get(p)
    assert code == 200 and body, p
for p in APIS:
    code, ctype, body = get(p)
    assert code == 200 and ctype.startswith("application/json"), (p, code, ctype)
    d = json.loads(body)
    assert "error" not in d, (p, d.get("error"))

# Only the listed static files are served, whatever the URL says.
for p in ("/static/page.js", "/static/../config.py", "/static/"):
    try:
        get(p)
    except urllib.error.HTTPError as e:
        assert e.code == 404, (p, e.code)
    else:
        raise AssertionError(f"{p} should be refused")

# The model form refuses anything but this portal's own page.
assert post("/api/models", headers={"Origin": "https://evil.example"})[0] == 403
assert post("/api/model", headers={"Content-Type": "text/plain"})[0] == 403
assert post("/api/watch", headers={"Origin": "https://evil.example"})[0] == 403
# An unknown job is refused, not started.
code, d = post("/job/nonesuch")
assert code == 409 and not d["started"], (code, d)

# Read-only mode: every write refused, setup and watchlist hidden, page told.
import os  # noqa: E402
from congress_trades.portal import jobs, pages  # noqa: E402
jobs.MODE["read_only"], os.environ["CONGRESS_HIDE_WATCHLIST"] = True, "1"
try:
    for p in ("/refresh", "/job/refresh", "/shutdown", "/api/watch", "/api/model"):
        assert post(p)[0] == 403, p
    for p in sorted(server.READ_ONLY_HIDDEN):
        try:
            get(p)
        except urllib.error.HTTPError as e:
            assert e.code == 404, (p, e.code)
        else:
            raise AssertionError(f"{p} should be hidden when read-only")
    assert b"READ_ONLY=true" in pages.app_html()
    assert get("/r/digest")[0] == 200 and get("/api/stats")[0] == 200
finally:
    jobs.MODE["read_only"] = False
    os.environ.pop("CONGRESS_HIDE_WATCHLIST", None)
assert b"READ_ONLY=false" in pages.app_html()

httpd.shutdown()
print(f"ok: {len(PAGES)} pages and {len(APIS)} API routes answer; static, origin, job "
      "and read-only guards refuse")
