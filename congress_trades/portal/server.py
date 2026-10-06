"""The HTTP server: routing, the same-origin guard, start and stop."""
from __future__ import annotations

import html
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs
from urllib.parse import quote
from urllib.parse import urlparse

from ..config import CONFIG
from .api import api_facets, api_member, api_names, api_ticker, api_timeline, api_top, api_trades, api_watch, stats, watch_set
from .jobs import JOBS, MODE, REPORTS, pidfile, _prereq, _refresh, _report_args, _run, start_job, start_refresh
from .models import PRESETS, model_current, model_list, model_save
from .pages import STATIC, WEB, app_html, jobs_page, md_to_html, overview, report_page, shell

HOST = "127.0.0.1"
PORT = 8777
# --- HTTP --------------------------------------------------------------------

# What a read-only portal will not show: the jobs, your model setup, a live model
# call, and your watchlist.
READ_ONLY_HIDDEN = {"/jobs", "/api/jobs", "/api/model", "/api/llm", "/api/watch"}


class Handler(BaseHTTPRequestHandler):
    server_version = "congress-portal"
    # Set once the server object exists, so a request can ask it to stop.
    srv = None

    def log_message(self, fmt, *a):            # one quiet line, not two noisy ones
        if not self.path.startswith(("/status", "/api/")):
            sys.stderr.write(f"  {self.command} {self.path}\n")

    def _send(self, body: bytes, ctype="text/html; charset=utf-8", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass                                # the tab moved on; not our problem

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, default=str).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    def _same_origin(self) -> bool:
        """Only this portal's own page may change the model or the watchlist.

        Two attacks to stop. A rebound DNS name -- an attacker's domain pointed at
        this machine -- arrives with the attacker's name in Host, so Host must be
        an address or one of this machine's own names. A page on another site
        posts with its own Origin, so Origin must match Host. The JSON content
        type forces a browser preflight, which this server never answers.

        Any IP literal is accepted, so another computer on your network can use
        the portal by address (http://192.168.x.x:8777) when it is bound there."""
        import ipaddress
        import socket
        host = self.headers.get("Host") or ""
        name = host.rsplit(":", 1)[0].strip("[]").lower()
        me = socket.gethostname().lower()
        try:
            ipaddress.ip_address(name)
            ok_host = True
        except ValueError:
            ok_host = name in ("localhost", me, f"{me}.local", f"{me}.lan")
        origin = self.headers.get("Origin")
        return (ok_host
                and (origin is None or origin.split("://", 1)[-1] == host)
                and (self.headers.get("Content-Type") or "").startswith("application/json"))

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 < n <= 8192:
            return {}
        try:
            d = json.loads(self.rfile.read(n))
        except ValueError:
            return {}
        return d if isinstance(d, dict) else {}

    def do_POST(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if MODE["read_only"]:
            self._json({"error": "this portal is read-only"}, 403)
            return
        if path == "/api/watch":
            if not self._same_origin():
                self._json({"error": "refused: not from this portal's page"}, 403)
                return
            try:
                self._json(watch_set(self._body()))
            except ValueError as e:
                self._json({"error": str(e)}, 400)
            return
        if path in ("/api/model", "/api/models"):
            if not self._same_origin():
                self._json({"error": "refused: not from this portal's page"}, 403)
                return
            from .. import llm
            try:
                if path == "/api/model":
                    self._json({"current": model_save(self._body())})
                else:
                    self._json({"models": model_list(self._body())})
            except ValueError as e:
                self._json({"error": str(e)}, 400)
            except llm.LLMError as e:
                self._json({"error": str(e)}, 502)
            return
        if path == "/refresh":
            started = start_refresh()
            self._json({"started": started, "running": _refresh["running"]})
            return
        if path.startswith("/job/"):
            name = path[len("/job/"):]
            since = (q.get("since") or [""])[0].strip()
            as_html = (q.get("html") or [""])[0] == "1"
            if as_html and not since:
                # The no-JavaScript form sends its fields in the body.
                n = int(self.headers.get("Content-Length") or 0)
                if 0 < n <= 4096:
                    body = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
                    since = (body.get("since") or [""])[0].strip()
            started, why = start_job(name, since)
            if as_html:
                self.send_response(303)
                self.send_header("Location",
                                 "/jobs" if started else "/jobs?msg=" + quote(why))
                self.end_headers()
                return
            self._json({"started": started, "why": why, "job": _refresh["job"],
                        "running": _refresh["running"]}, 200 if started else 409)
            return
        if path == "/shutdown":
            # A refresh is a long, resumable job, but stopping mid-`prices` throws
            # away an uncommitted transaction -- so say so rather than silently
            # killing it. The caller re-posts with ?force=1 to mean it.
            force = (parse_qs(urlparse(self.path).query).get("force") or [""])[0] == "1"
            if _refresh["running"] and not force:
                self._json({"stopped": False, "refreshing": True,
                            "note": "a refresh is running; stopping now discards its "
                                    "uncommitted pass"}, 409)
                return
            self._json({"stopped": True})
            threading.Thread(target=_shutdown, daemon=True).start()
            return
        self._json({"error": "not found"}, 404)

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if MODE["read_only"] and path in READ_ONLY_HIDDEN:
            self._json({"error": "not available on a read-only portal"}, 404)
            return

        if path.startswith("/static/") and path[8:] in STATIC:
            self._send((WEB / path[8:]).read_bytes(), STATIC[path[8:]])
            return
        if path == "/status":
            job = _refresh["job"]
            self._json({"running": _refresh["running"], "line": _refresh["line"],
                        "rc": _refresh["rc"], "job": job,
                        "title": JOBS.get(job, {}).get("title", job),
                        "verb": JOBS.get(job, {}).get("verb", "Running"),
                        "elapsed": int(time.time() - _refresh["started"])
                        if _refresh["started"] else 0})
            return
        if path == "/api/watch":
            self._json(api_watch())
            return
        if path == "/feed.xml":
            from .. import alerts
            here = f"http://{self.headers.get('Host') or f'{HOST}:{PORT}'}/"
            self._send(alerts.to_rss(alerts.feed(cfg=CONFIG), here).encode("utf-8"),
                       "application/rss+xml; charset=utf-8")
            return
        if path == "/api/model":
            self._json({"presets": PRESETS, "current": model_current()})
            return
        if path == "/api/jobs":
            pre = _prereq()
            self._json({
                "jobs": [{"name": k, "title": v["title"], "blurb": v["blurb"],
                          "needs": list(v["needs"]), "since": bool(v.get("since")),
                          "ready": all(pre[n]["ok"] for n in v["needs"]),
                          "why": next((pre[n]["why"] for n in v["needs"]
                                       if not pre[n]["ok"]), "")}
                         for k, v in JOBS.items()],
                "prereq": pre,
                "running": _refresh["running"], "current": _refresh["job"]})
            return
        if path == "/api/llm":
            # The live round trip, not the shape checks: it is the only thing
            # that catches an endpoint which answers but ignores a JSON schema,
            # which is exactly what resolve and topics depend on.
            rc, out = _run(["llm"], timeout=180)
            self._json({"rc": rc, "text": out})
            return
        if path == "/api/stats":
            self._json(stats()); return
        if path == "/api/names":
            self._json(api_names()); return
        if path == "/api/facets":
            self._json(api_facets()); return
        if path == "/api/trades":
            self._json(api_trades(q)); return
        if path == "/api/top":
            self._json(api_top(q)); return
        if path == "/api/timeline":
            self._json(api_timeline(q)); return
        if path == "/api/ticker":
            self._json(api_ticker((q.get("symbol") or [""])[0], q)); return
        if path == "/api/member":
            self._json(api_member((q.get("name") or [""])[0])); return
        if path.startswith("/api/report/"):
            name = path[len("/api/report/"):]
            if name not in REPORTS:
                self._json({"error": "unknown report"}, 404); return
            rc, out = _run([name, *_report_args(name, q)])
            # Some reports are aligned columns rather than markdown, and running
            # those through the table parser collapses the alignment that IS the
            # output. They declare that in REPORTS rather than being guessed at.
            body = (f"<pre>{html.escape(out)}</pre>" if REPORTS[name].get("pre")
                    else md_to_html(out))
            self._json({"rc": rc, "html": body, "text": out,
                        "refreshing": _refresh["running"]})
            return

        if path == "/":
            self._send(app_html())
            return
        if path == "/page":
            f = CONFIG.out_html
            if not f.exists():
                self._send(shell("Full page",
                                 '<div class="err">The rendered page does not exist yet. '
                                 'Refresh to build it.</div>', "page"))
                return
            self._send(f.read_bytes())
            return

        # Server-rendered reports: the no-JavaScript fallback, and what a bookmark
        # or `curl` gets. The app above uses /api/report/* instead.
        if path.startswith("/r/"):
            name = path[3:]
            if name not in REPORTS:
                self._send(shell("Not found", '<div class="err">No such report.</div>'), code=404)
                return
            self._send(shell(REPORTS[name]["title"], report_page(name, q), name))
            return
        if path == "/jobs":
            self._send(shell("Maintenance", jobs_page(
                (q.get("msg") or [""])[0][:200]), "jobs"))
            return
        if path == "/overview":
            self._send(shell("Congress Trades", overview(), ""))
            return

        self._send(shell("Not found", '<div class="err">No such page.</div>'), code=404)


def _shutdown() -> None:
    """Called off the request thread: shutdown() blocks until serve_forever returns,
    and serve_forever cannot return while it is waiting on the handler calling it.

    A refresh subprocess would outlive us and keep the database locked with nothing
    watching it, so it is stopped first. Losing it costs nothing that is not
    resumable: filings and price series are already cached on disk, and the pass
    that dies was uncommitted anyway."""
    time.sleep(0.2)
    stop_refresh()
    if Handler.srv is not None:
        Handler.srv.shutdown()


def stop_refresh() -> None:
    p = _refresh.get("proc")
    if p is None or p.poll() is not None:
        return
    try:
        p.terminate()
        try:
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()
    except OSError:
        pass


def stop(quiet: bool = False, port: int = PORT) -> int:
    """Signal a running portal, identified by its port's pid file."""
    import os
    import signal
    PIDFILE = pidfile(port)
    if not PIDFILE.exists():
        if not quiet:
            print("no portal is running (no pid file)")
        return 1
    try:
        pid = int(PIDFILE.read_text().strip())
    except (ValueError, OSError):
        PIDFILE.unlink(missing_ok=True)
        print("stale pid file removed")
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        PIDFILE.unlink(missing_ok=True)
        print(f"no process {pid}; stale pid file removed")
        return 1
    except PermissionError:
        print(f"not allowed to signal {pid}")
        return 1
    for _ in range(40):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            print(f"stopped {pid}")
            return 0
    print(f"{pid} did not exit; send SIGKILL yourself if you mean it")
    return 1


def serve(host: str = HOST, port: int = PORT, read_only: bool = False) -> int:
    """read_only refuses every write and hides your setup and watchlist, for
    sharing: no refresh, jobs, stop, model form or watchlist, in the server and the
    page alike, and the watchlist is hidden from reports and the feed too."""
    import os
    PIDFILE = pidfile(port)
    MODE["read_only"] = read_only
    if read_only:
        os.environ["CONGRESS_HIDE_WATCHLIST"] = "1"     # this process and every report
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"cannot bind {host}:{port}: {e}")
        if PIDFILE.exists():
            print(f"  another portal may already be running -- "
                  f"`./run.sh portal stop` (pid {PIDFILE.read_text().strip()})")
        return 1
    Handler.srv = httpd
    try:
        PIDFILE.write_text(str(__import__("os").getpid()))
    except OSError:
        pass
    s = stats()
    print(f"portal: http://{host}:{port}" + ("  (read-only)" if read_only else ""))
    if s and "error" not in s:
        print(f"  {s.get('trades', 0):,} trades, {s.get('members', 0):,} members, "
              f"{s.get('priced', 0):,} priced")
    else:
        print("  no database yet -- hit Refresh in the portal, or run ./run.sh")
    if not CONFIG.contact:
        print("  warning: CONGRESS_CONTACT is unset, so a refresh will stop at `sectors`."
              "\n           Start via ./run.sh portal, or export it first.")
    print("  ctrl-c to stop")
    import signal
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(
        target=httpd.shutdown, daemon=True).start())
    try:
        httpd.serve_forever()
        print("stopped")
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        stop_refresh()
        httpd.server_close()
        PIDFILE.unlink(missing_ok=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    host, port, read_only = HOST, PORT, "--read-only" in argv
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
        elif a.startswith("--port="):
            port = int(a.split("=", 1)[1])
        elif a == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
        elif a.startswith("--host="):
            host = a.split("=", 1)[1]
    if argv and argv[0] in ("stop", "--stop"):
        return stop(port=port)
    return serve(host, port, read_only)



