"""Dashboard users and sessions. Standard library only.

Users live in the DASHBOARD_USERS env var as comma-separated  name:hash  pairs. Make a hash with:

    python auth.py            # prompts for a password, prints the hash to paste after "name:"

Sessions are signed cookies (HMAC with SESSION_SECRET): no server-side store, so they survive restarts.
Changing SESSION_SECRET signs everyone out; removing a user from DASHBOARD_USERS revokes their session.
"""
import base64
import hashlib
import hmac
import os
import re
import secrets
import time

ITERATIONS = 600_000
SESSION_HOURS = 12
USERNAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_DUMMY = None          # hash checked for unknown users, so a wrong name takes as long as a wrong password


def hash_password(password, iterations=ITERATIONS):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${digest.hex()}"


def _check(password, stored):
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != "pbkdf2":
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(got.hex(), digest)
    except ValueError:
        return False


def load_users(spec=None):
    """{name: hash} from DASHBOARD_USERS. Raises ValueError on a malformed entry, so a typo fails at startup."""
    spec = os.environ.get("DASHBOARD_USERS", "") if spec is None else spec
    users = {}
    for entry in filter(None, (e.strip() for e in spec.split(","))):
        name, sep, stored = entry.partition(":")
        if not sep or not USERNAME.match(name) or not stored.startswith("pbkdf2$"):
            raise ValueError(f"DASHBOARD_USERS: bad entry for {name!r}; expected name:pbkdf2$...")
        users[name] = stored
    return users


def verify_user(users, name, password):
    global _DUMMY
    stored = users.get(name)
    if stored is None:
        _DUMMY = _DUMMY or hash_password(secrets.token_hex(8))
        _check(password, _DUMMY)
        return False
    return _check(password, stored)


def _sign(secret, payload):
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_session(secret, name, hours=SESSION_HOURS):
    payload = base64.urlsafe_b64encode(f"{name}|{int(time.time() + hours * 3600)}".encode()).decode()
    return f"{payload}.{_sign(secret, payload)}"


def read_session(secret, users, token):
    """The signed-in user's name, or None if the token is missing, forged, expired or the user was removed."""
    try:
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(secret, payload)):
            return None
        name, expires = base64.urlsafe_b64decode(payload).decode().rsplit("|", 1)
        return name if int(expires) > time.time() and name in users else None
    except (ValueError, AttributeError):
        return None


class Throttle:
    """At most `limit` failed logins per key in `window` seconds (in memory, per process)."""

    def __init__(self, limit=5, window=900):
        self.limit, self.window, self.fails = limit, window, {}

    def blocked(self, key):
        now = time.time()
        self.fails[key] = [t for t in self.fails.get(key, []) if t > now - self.window]
        return len(self.fails[key]) >= self.limit

    def fail(self, key):
        self.fails.setdefault(key, []).append(time.time())

    def clear(self, key):
        self.fails.pop(key, None)


if __name__ == "__main__":
    import getpass
    pw = getpass.getpass("Password (12+ characters): ")
    if len(pw) < 12:
        raise SystemExit("Too short: use at least 12 characters.")
    if pw != getpass.getpass("Again: "):
        raise SystemExit("Passwords don't match.")
    print(hash_password(pw))
