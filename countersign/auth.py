"""Approver accounts and login sessions (stdlib only).

- Accounts live in <data_dir>/approvers.json (gitignored, mode 600), created with
  `python -m countersign add-approver <name>`. Passwords are stored as salted scrypt hashes.
- A session is server-side (in memory): the browser only holds a random session id, signed with
  HMAC-SHA256 under a secret generated at first start (<data_dir>/session.key, mode 600).
  Cookie: HttpOnly, SameSite=Strict. Each session has its own CSRF token.
- Failed logins are slowed down: password checks run one at a time (a global gate, so parallel guesses
  are not faster) with a fixed delay after each failure; an account is locked after 5 failures, a client
  address after 20, both for 15 minutes. The failure store is capped.
- Removing an approver (CLI) ends that approver's sessions at the next request.
- The approver identity used for every decision comes from the session, never from a form field.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

AUTH_METHOD = "password"  # what a web login proves: a local approver account + its password
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
RESERVED = {"agent", "system", "executor", "human", "admin", "root"}
SCRYPT = {"n": 2 ** 17, "r": 8, "p": 1}  # 128 MiB, about 0.2 s per check
COOKIE = "cs_session"
SESSION_TTL = 8 * 3600
LOGIN_TOKEN_TTL = 600
MAX_FAILS, LOCK_SECS = 5, 15 * 60
MAX_CLIENT_FAILS = 20  # per client address per LOCK_SECS
MAX_TRACKED = 1000  # cap on the failure store (names + client addresses)
SCRYPT_MAXMEM = 256 * 1024 * 1024


class AuthError(RuntimeError):
    pass


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, dklen=32, maxmem=SCRYPT_MAXMEM, **SCRYPT)
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${_b64(salt)}${_b64(dk)}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, want = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=32,
                            maxmem=SCRYPT_MAXMEM)
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(dk, _unb64(want))


_DUMMY = hash_password(secrets.token_urlsafe(16))  # unknown users cost the same scrypt as known ones


def _eq(a: str, b: str) -> bool:
    """Constant-time compare that never raises: hmac.compare_digest(str, str) raises on non-ASCII text."""
    return hmac.compare_digest((a or "").encode("utf-8", "surrogatepass"), (b or "").encode("utf-8", "surrogatepass"))


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


class Approvers:
    """The approver account file."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "approvers.json"

    def _load(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def names(self) -> list[str]:
        return sorted(self._load())

    def add(self, name: str, password: str) -> None:
        name = name.strip().lower()
        if not NAME_RE.match(name) or name in RESERVED:
            raise AuthError("name must be 2-32 chars [a-z0-9._-] and not agent/system/executor/human/admin/root")
        if len(password) < 12:
            raise AuthError("password must be at least 12 characters")
        accounts = self._load()
        accounts[name] = {"hash": hash_password(password), "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        _write_private(self.path, json.dumps(accounts, indent=1))

    def remove(self, name: str) -> bool:
        accounts = self._load()
        if accounts.pop(name.strip().lower(), None) is None:
            return False
        _write_private(self.path, json.dumps(accounts, indent=1))
        return True

    def exists(self, name: str) -> bool:
        return (name or "").strip().lower() in self._load()

    def check(self, name: str, password: str) -> bool:
        acc = self._load().get(name.strip().lower())
        ok = check_password(password, acc["hash"] if acc else _DUMMY)
        return bool(acc) and ok


@dataclass
class Session:
    sid: str
    user: str
    csrf: str
    expires: float
    auth_method: str = AUTH_METHOD
    flash: str = ""  # one-time message for the next page view (server-side, never from the URL)


class Auth:
    """Login, sessions, CSRF and failed-login throttling for the web UI."""

    def __init__(self, data_dir: Path, failure_delay: float = 1.0):
        self.approvers = Approvers(data_dir)
        key_path = Path(data_dir) / "session.key"
        if not key_path.exists():
            _write_private(key_path, secrets.token_hex(32))
        self.secret = bytes.fromhex(key_path.read_text().strip())
        self.failure_delay = failure_delay
        self._sessions: dict[str, Session] = {}
        self._fails: dict[str, list[float]] = {}  # "user:<name>" / "client:<addr>" -> failure times
        self._lock = threading.Lock()
        self._gate = threading.Lock()  # one password check at a time (global throttle)

    # ------------------------------------------------------------ cookie signing
    def _sign(self, value: str) -> str:
        return _b64(hmac.new(self.secret, value.encode(), hashlib.sha256).digest())

    def cookie_value(self, s: Session) -> str:
        return f"{s.sid}.{self._sign('session:' + s.sid)}"

    def login_token(self) -> str:
        """Token for the login form itself (login CSRF): time-stamped and signed, valid 10 minutes."""
        ts = str(int(time.time()))
        return f"{ts}.{self._sign('login:' + ts)}"

    def check_login_token(self, token: str) -> bool:
        ts, _, sig = (token or "").partition(".")
        if not ts.isdigit() or not ts.isascii() or not _eq(sig, self._sign("login:" + ts)):
            return False
        return 0 <= time.time() - int(ts) <= LOGIN_TOKEN_TTL

    # ------------------------------------------------------------ login / sessions
    def _recent(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._fails.get(key, []) if now - t < LOCK_SECS]
        if recent:
            self._fails[key] = recent
        else:
            self._fails.pop(key, None)
        return recent

    def _note_failure(self, keys: list[str]) -> None:
        now = time.time()
        with self._lock:
            for k in keys:
                self._fails.setdefault(k, []).append(now)
            if len(self._fails) > MAX_TRACKED:  # drop expired keys first, then the oldest ones
                for k in list(self._fails):
                    self._recent(k, now)
                while len(self._fails) > MAX_TRACKED:
                    oldest = min(self._fails, key=lambda k: self._fails[k][-1])
                    self._fails.pop(oldest)

    def locked(self, name: str, client: str | None = None) -> bool:
        now = time.time()
        with self._lock:
            if len(self._recent("user:" + name, now)) >= MAX_FAILS:
                return True
            return bool(client) and len(self._recent("client:" + client, now)) >= MAX_CLIENT_FAILS

    def login(self, name: str, password: str, client: str | None = None) -> Session:
        name = (name or "").strip().lower()
        with self._gate:  # serialises password checks + failure delays across all request threads
            if self.locked(name, client):
                time.sleep(self.failure_delay)
                raise AuthError("too many failed logins; try again in 15 minutes")
            if not name or not password or not self.approvers.check(name, password):
                self._note_failure(["user:" + name] + (["client:" + client] if client else []))
                time.sleep(self.failure_delay)
                raise AuthError("wrong name or password")
        with self._lock:
            self._fails.pop("user:" + name, None)
            s = Session(secrets.token_urlsafe(32), name, secrets.token_urlsafe(24), time.time() + SESSION_TTL)
            self._sessions[s.sid] = s
            return s

    def audit_name(self, typed: str) -> str:
        """What a failed-login audit entry may record: the name if it is an existing account, else
        'unknown user #<keyed hash>' (a password typed into the name field must never reach the log)."""
        name = (typed or "").strip().lower()
        if NAME_RE.match(name) and self.approvers.exists(name):
            return name
        return "unknown user #" + self._sign("audit-name:" + (typed or ""))[:12]

    def session(self, cookie_header: str | None) -> Session | None:
        value = None
        for part in (cookie_header or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                value = v
        if not value:
            return None
        sid, _, sig = value.partition(".")
        if not sid or not _eq(sig, self._sign("session:" + sid)):
            return None
        with self._lock:
            s = self._sessions.get(sid)
            if s and s.expires < time.time():
                self._sessions.pop(sid, None)
                return None
        if s and not self.approvers.exists(s.user):  # approver removed: end all of their sessions
            self.end_sessions(s.user)
            return None
        return s

    def end_sessions(self, user: str) -> int:
        with self._lock:
            gone = [sid for sid, x in self._sessions.items() if x.user == user]
            for sid in gone:
                self._sessions.pop(sid, None)
            return len(gone)

    def logout(self, s: Session) -> None:
        with self._lock:
            self._sessions.pop(s.sid, None)

    @staticmethod
    def csrf_ok(s: Session | None, token: str) -> bool:
        return bool(s) and bool(token) and _eq(token, s.csrf)
