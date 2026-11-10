"""HTTP entry point for deployment (Railway sets PORT).

    GET  /health      -> {"status": "ok"}                    no auth, for the platform health check
    POST /triage      -> triage() result as JSON             needs  Authorization: Bearer $API_TOKEN
                         body: {"email": "<complaint text>", "sender": "<customer email>"}
    GET  /login       -> sign-in form                        dashboard users, see auth.py
    POST /login, /logout
    GET  /dashboard   -> live dashboard, signed-in users only; each view is logged with the user's name

`sender` is trusted by the ownership check, so only a trusted caller (your mail/ticket system) may hold
API_TOKEN. Requests are handled one at a time: the tracer shares one SQLite connection and span stack.
"""
import hmac
import html
import json
import os
import sys
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from dotenv import load_dotenv

load_dotenv()

import agent  # noqa: E402  (after load_dotenv so model settings are read)
import auth  # noqa: E402
from build_dashboard import DashboardError, render  # noqa: E402

API_TOKEN = os.environ.get("API_TOKEN", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
USERS = auth.load_users()
COOKIE = "session"
MAX_BODY = 64 * 1024
throttle = auth.Throttle()

HTML_HEADERS = {
    "Content-Type": "text/html; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",   # not "no-referrer": that makes browsers send Origin: null on form posts
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                               "frame-ancestors 'none'; base-uri 'none'",
}

LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sign in</title>
<style>
:root {{ --bg:#f5f6f8; --card:#fff; --ink:#1b2330; --muted:#5d6878; --line:#dfe3ea; --accent:#2a78d6; --alert:#d03b3b }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#14171c; --card:#1d2128; --ink:#e8ebf0; --muted:#9aa4b2;
  --line:#2f3540; --accent:#4b93e8; --alert:#ef6b6b }} }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; padding:16px; box-sizing:border-box }}
form {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:24px; width:min(100%,340px);
  display:grid; gap:12px }}
h1 {{ margin:0; font-size:20px }} label {{ display:grid; gap:4px; font-size:13px; color:var(--muted) }}
input {{ font:inherit; padding:8px 10px; border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--ink) }}
button {{ font:inherit; padding:9px; border:0; border-radius:6px; background:var(--accent); color:#fff; cursor:pointer }}
.err {{ color:var(--alert); margin:0; font-size:14px }}
</style></head><body>
<form method="post" action="/login"><h1>Merlion triage health</h1>{error}
<label>Username<input name="username" autocomplete="username" required autofocus></label>
<label>Password<input name="password" type="password" autocomplete="current-password" required></label>
<button>Sign in</button></form></body></html>"""

USER_BAR = ('<div style="display:flex;justify-content:flex-end;align-items:center;gap:10px;font-size:13px">'
            '<span class="muted">Signed in as <b>{name}</b></span>'
            '<form method="post" action="/logout" style="margin:0"><button style="font:inherit;font-size:13px;'
            'padding:4px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);'
            'color:var(--ink);cursor:pointer">Sign out</button></form></div>')


class Handler(BaseHTTPRequestHandler):
    # ---------- helpers ----------
    def _send(self, code, body, headers=None):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        for k, v in (headers or {"Content-Type": "application/json"}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, where, cookie=None):
        self.send_response(303)
        self.send_header("Location", where)
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            return None
        return self.rfile.read(length)

    def _same_origin(self):
        """Block cross-site form posts (login CSRF). A cross-site page can't forge these headers."""
        site = self.headers.get("Sec-Fetch-Site")          # sent by all current browsers
        if site:
            return site in ("same-origin", "none")
        origin = self.headers.get("Origin")
        if origin is None:                                  # curl, scripts: not a browser CSRF
            return True
        hosts = {self.headers.get("Host"), self.headers.get("X-Forwarded-Host")}   # behind Railway's proxy
        return urlsplit(origin).netloc in hosts - {None}

    def _user(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return auth.read_session(SESSION_SECRET, USERS, cookie[COOKIE].value) if COOKIE in cookie else None

    def _login_page(self, code=200, error=""):
        err = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
        self._send(code, LOGIN_PAGE.format(error=err), HTML_HEADERS)

    @property
    def path_only(self):
        return urlsplit(self.path).path

    # ---------- routes ----------
    def do_GET(self):
        path = self.path_only
        if path == "/health":
            return self._send(200, {"status": "ok"})
        if path in ("/", "/login", "/dashboard") and not USERS:
            return self._send(404, {"error": "dashboard disabled: no DASHBOARD_USERS configured"})
        if path == "/":
            return self._redirect("/dashboard")
        if path == "/login":
            return self._redirect("/dashboard") if self._user() else self._login_page()
        if path == "/dashboard":
            user = self._user()
            if not user:
                return self._redirect("/login")
            try:
                page, _ = render()
            except DashboardError as e:
                msg = str(e) if "No traces" in str(e) else "Dashboard withheld: personal data found in the page."
                self.log_error("dashboard not shown to %s: %s", user, msg)
                return self._send(503, f"<p>{html.escape(msg)}</p>", HTML_HEADERS)
            self.log_message("dashboard viewed by %s", user)
            page = page.replace("<main>", "<main>" + USER_BAR.format(name=html.escape(user)), 1)
            return self._send(200, page, HTML_HEADERS)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path_only
        if path == "/triage":
            return self._triage()
        if path in ("/login", "/logout") and not USERS:
            return self._send(404, {"error": "dashboard disabled: no DASHBOARD_USERS configured"})
        if path in ("/login", "/logout") and not self._same_origin():
            return self._send(403, {"error": "cross-site request refused"})
        if path == "/logout":
            return self._redirect("/login", f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict")
        if path == "/login":
            return self._login()
        self._send(404, {"error": "not found"})

    def _login(self):
        body = self._body()
        form = parse_qs(body.decode(errors="replace")) if body else {}
        name = form.get("username", [""])[0].strip()
        password = form.get("password", [""])[0]
        if not name or not password:
            return self._login_page(400, "Enter your username and password.")
        key = name.lower()
        if throttle.blocked(key):
            self.log_error("login throttled for %r", name[:64])
            return self._login_page(429, "Too many failed attempts. Try again in 15 minutes.")
        if not auth.verify_user(USERS, name, password):
            throttle.fail(key)
            self.log_error("failed login for %r", name[:64])
            return self._login_page(401, "Wrong username or password.")
        throttle.clear(key)
        self.log_message("login: %s", name)
        token = auth.make_session(SESSION_SECRET, name)
        self._redirect("/dashboard", f"{COOKIE}={token}; Path=/; Max-Age={auth.SESSION_HOURS * 3600}; "
                                     "HttpOnly; Secure; SameSite=Strict")

    def _triage(self):
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(token.encode(), API_TOKEN.encode()):
            return self._send(401, {"error": "unauthorized"})
        raw = self._body()
        if raw is None:
            return self._send(400, {"error": "body must be 1 byte to 64 KB of JSON"})
        try:
            body = json.loads(raw)
            email, sender = body["email"], body["sender"]
            if not (isinstance(email, str) and isinstance(sender, str) and email and sender):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return self._send(400, {"error": 'expected {"email": "...", "sender": "..."}'})
        try:
            self._send(200, agent.triage(email, sender))
        except Exception as e:
            self.log_error("triage failed: %s", type(e).__name__)   # type only: the message may hold PII
            self._send(500, {"error": "internal error"})

    def log_message(self, fmt, *args):                 # paths, statuses and usernames only, never bodies
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    if not API_TOKEN:
        sys.exit("API_TOKEN is not set; refusing to start an unauthenticated server.")
    if USERS and len(SESSION_SECRET) < 32:
        sys.exit("DASHBOARD_USERS is set but SESSION_SECRET is missing or shorter than 32 characters.")
    port = int(os.environ.get("PORT", "8000"))
    print(f"Listening on :{port} · dashboard {'enabled for ' + str(len(USERS)) + ' user(s)' if USERS else 'disabled'}",
          flush=True)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
