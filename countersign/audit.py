"""Append-only, hash-chained audit log (JSON Lines).

Every entry stores the SHA-256 of the previous entry, so editing or deleting a past line
breaks the chain. `verify()` re-walks the file and reports the first broken link.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def _digest(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

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
            prev = entry["hash"]
        return True, "chain intact"
