"""Railway web service: serves both dashboards over the public internet with basic auth.

  /            production dashboard (Priya / CS team)
  /dev         developer dashboard
  /health      health check for Railway

Env:
    PORT                      set by Railway
    OPS_DASH_USER/PASS        required for /
    DEV_DASH_USER/PASS        required for /dev (defaults to ops creds if unset)
    ENABLE_DEV_DASHBOARD      true/false (default true)
    MERLION_SOURCE            fixture (default) or mysql, with MYSQL_URL / DB_* (see SETUP.md)
"""
import base64
import http.server
import os
import subprocess
import sys
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
# out/ and out_dev/ are gitignored, so a fresh deploy has no dashboards: build them on startup.
BUILD_STEPS = [["run_demo.py"], ["build_dashboard.py"], ["simulate_dev.py"], ["build_dev_dashboard.py"]]
BUILD = {"state": "idle", "error": ""}
ROUTES = {"/": "out/dashboard.html", "/dev": "out_dev/dev_dashboard.html"}
OPS_USER, OPS_PASS = os.environ.get("OPS_DASH_USER"), os.environ.get("OPS_DASH_PASS")
DEV_USER = os.environ.get("DEV_DASH_USER") or OPS_USER
DEV_PASS = os.environ.get("DEV_DASH_PASS") or OPS_PASS
ENABLE_DEV = (os.environ.get("ENABLE_DEV_DASHBOARD", "true").strip().lower() == "true")


def _auth_ok(headers, user, pwd):
    if not user or not pwd:
        return False
    got = headers.get("Authorization")
    want = "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()
    return got == want


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            return self._send(200, b"ok", "text/plain")

        if path == "/dev" and not ENABLE_DEV:
            return self._send(404, b"not found", "text/plain")

        if path == "/":
            if not OPS_USER or not OPS_PASS:
                return self._send(503, b"Set OPS_DASH_USER and OPS_DASH_PASS.", "text/plain")
            if not _auth_ok(self.headers, OPS_USER, OPS_PASS):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="merlion-ops"')
                self.end_headers()
                return
        elif path == "/dev":
            if not DEV_USER or not DEV_PASS:
                return self._send(503, b"Set DEV_DASH_USER and DEV_DASH_PASS.", "text/plain")
            if not _auth_ok(self.headers, DEV_USER, DEV_PASS):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="merlion-dev"')
                self.end_headers()
                return
        else:
            return self._send(404, b"not found", "text/plain")

        f = ROUTES.get(path)
        if not f:
            return self._send(404, b"not found", "text/plain")
        full = os.path.join(ROOT, f)
        if not os.path.exists(full):
            if BUILD["state"] == "failed":
                return self._send(500, ("Dashboard build failed:\n\n" + BUILD["error"]).encode(), "text/plain; charset=utf-8")
            return self._send(503, b"Dashboards are still being built. Refresh in a minute.", "text/plain")
        with open(full, "rb") as fh:
            self._send(200, fh.read(), "application/json" if f.endswith(".json") else "text/html; charset=utf-8")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def build_dashboards():
    if all(os.path.exists(os.path.join(ROOT, f)) for f in ROUTES.values()):
        BUILD["state"] = "done"
        return
    BUILD["state"] = "building"
    os.makedirs(os.path.join(ROOT, "out"), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "out_dev"), exist_ok=True)
    for step in BUILD_STEPS:
        print(f"build: {step[0]}", flush=True)
        r = subprocess.run([sys.executable, *step], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        if r.returncode != 0:
            BUILD["state"] = "failed"
            BUILD["error"] = f"{step[0]} exited {r.returncode}\n{r.stderr[-4000:]}"
            print("build failed:\n" + BUILD["error"], flush=True)
            return
    BUILD["state"] = "done"
    print("build: dashboards ready", flush=True)


if __name__ == "__main__":
    threading.Thread(target=build_dashboards, daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    print(f"serving on :{port}", flush=True)
    http.server.ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
