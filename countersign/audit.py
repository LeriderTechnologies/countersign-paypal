"""Append-only, hash-chained audit log (JSON Lines), signed with a server key.

Every entry stores the SHA-256 of the previous entry, so editing or deleting a past line
breaks the chain. Every entry also carries an HMAC-SHA256 of its hash under a server key
(<data_dir>/audit.key, created mode 600 on first run, never committed), and every approval
carries an HMAC-SHA256 approval digest under the same key. So someone who can edit the log
file but does not have the key cannot rewrite a line and re-hash the rest of the chain:
the HMACs no longer match. Limit: whoever can read audit.key can re-sign a rewritten log;
a real deployment keeps the key in a secrets store / HSM or anchors the chain head externally.
`verify()` re-walks the file and reports the first broken link or signature.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def _canon(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def load_or_create_key(path: Path) -> bytes:
    """Read the audit signing key, or create it (32 random bytes, hex, mode 600) if it does not exist."""
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:  # created by a parallel process in the meantime
            pass
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(secrets.token_hex(32))
    os.chmod(path, 0o600)
    return bytes.fromhex(path.read_text().strip())


def approval_digest(key: bytes, card: str, card_digest: str, approver: str, auth_method: str, approved_at: str) -> str:
    """HMAC-SHA256 (server key) over the card, the exact content shown (card digest), who approved,
    how they proved it, and when."""
    body = {"card": card, "card_digest": card_digest, "approver": approver, "auth_method": auth_method,
            "approved_at": approved_at}
    return hmac.new(key, _canon(body), hashlib.sha256).hexdigest()


def _digest(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k not in ("hash", "mac")}
    return hashlib.sha256(_canon(body)).hexdigest()


def _mac(key: bytes, entry_hash: str) -> str:
    return hmac.new(key, ("audit-entry:" + entry_hash).encode(), hashlib.sha256).hexdigest()


class AuditLog:
    def __init__(self, path: Path, key_path: Path | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.key_path = Path(key_path) if key_path else self.path.parent / "audit.key"
        self.key = load_or_create_key(self.key_path)
        self._lock = threading.Lock()

    def approval_digest(self, card: str, card_digest: str, approver: str, auth_method: str, approved_at: str) -> str:
        return approval_digest(self.key, card, card_digest, approver, auth_method, approved_at)

    def _approval_ok(self, entry: dict) -> bool:
        d = entry.get("details") or {}
        if entry.get("event") != "card_approved":
            return True
        if "approval_digest" not in d:
            return False
        want = self.approval_digest(d.get("card", ""), d.get("digest", ""), d.get("approver", ""),
                                    d.get("auth_method", ""), d.get("approved_at", ""))
        return d.get("approver") == entry.get("actor") and hmac.compare_digest(want.encode(), str(d["approval_digest"]).encode())

    def _last_hash(self) -> str:
        if not self.path.exists():
            return GENESIS
        last = GENESIS
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = json.loads(line)["hash"]
        return last

    def record(self, event: str, actor: str, **details) -> dict:
        with self._lock:
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "event": event,
                "actor": actor,
                "details": details,
                "prev": self._last_hash(),
            }
            entry["hash"] = _digest(entry)
            entry["mac"] = _mac(self.key, entry["hash"])
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
            return entry

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def verify(self) -> tuple[bool, str]:
        prev = GENESIS
        for i, entry in enumerate(self.entries(), start=1):
            if entry.get("prev") != prev:
                return False, f"line {i}: prev-hash mismatch (an entry before it was changed or removed)"
            if _digest(entry) != entry.get("hash"):
                return False, f"line {i}: content does not match its hash (entry was edited)"
            if not hmac.compare_digest(_mac(self.key, str(entry["hash"])).encode(), str(entry.get("mac", "")).encode()):
                return False, f"line {i}: server-key signature (HMAC) missing or wrong (entry rewritten without the key)"
            if not self._approval_ok(entry):
                return False, f"line {i}: approval digest (HMAC) does not match approver/auth method (entry was edited)"
            prev = entry["hash"]
        return True, "chain intact"
