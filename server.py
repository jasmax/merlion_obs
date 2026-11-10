"""HTTP entry point for deployment (Railway sets PORT).

    GET  /health   -> {"status": "ok"}                       no auth, for the platform health check
    POST /triage   -> triage() result as JSON                needs  Authorization: Bearer $API_TOKEN
                      body: {"email": "<complaint text>", "sender": "<customer email>"}

`sender` is trusted by the ownership check, so only a trusted caller (your mail/ticket system) may hold
API_TOKEN. Requests are handled one at a time: the tracer shares one SQLite connection and span stack.
"""
import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv

load_dotenv()

import agent  # noqa: E402  (after load_dotenv so model settings are read)

API_TOKEN = "5ZYZyeCGgJXLQ2asGyaffSsBjz3VVXFedDiSllCyY9o"
MAX_BODY = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/triage":
            return self._send(404, {"error": "not found"})
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(token.encode(), API_TOKEN.encode()):
            return self._send(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            return self._send(413 if length else 400, {"error": "body must be 1 byte to 64 KB of JSON"})
        try:
            body = json.loads(self.rfile.read(length))
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

    def log_message(self, fmt, *args):                 # path and status only, never bodies
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    if not API_TOKEN:
        sys.exit("API_TOKEN is not set; refusing to start an unauthenticated server.")
    port = int(os.environ.get("PORT", "8000"))
    print(f"Listening on :{port}", flush=True)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
