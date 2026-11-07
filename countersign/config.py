"""Configuration. Reads environment variables and an optional .env file.

PayPal mode is decided here and nowhere else:
  - no PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET  -> MOCK backend (default)
  - both present                                 -> PayPal REST *sandbox*
There is deliberately no way to select the live API.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SANDBOX_HOST = "api-m.sandbox.paypal.com"
SANDBOX_BASE_URL = f"https://{SANDBOX_HOST}"


class ConfigError(RuntimeError):
    pass


def load_dotenv(path: Path) -> dict[str, str]:
    """Tiny .env parser (KEY=VALUE, # comments). Does not override real env vars."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(f"[countersign] warning: {path} is readable by others; run: chmod 600 {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    paypal_client_id: str
    paypal_client_secret: str
    anthropic_api_key: str
    anthropic_model: str
    planner_choice: str = ""  # "", "mock", "replay" (COUNTERSIGN_PLANNER)
    replay_path: Path = ROOT / "docs" / "claude_run.json"
    # Sandbox: the supplier must be a real sandbox *business* account, or the buyer cannot pay the order.
    supplier_email: str = ""  # COUNTERSIGN_SUPPLIER_EMAIL; empty = the fictional catalogue address

    @property
    def paypal_mode(self) -> str:
        return "sandbox" if (self.paypal_client_id and self.paypal_client_secret) else "mock"

    @property
    def planner_mode(self) -> str:
        if self.planner_choice in ("mock", "replay"):
            return self.planner_choice
        return "claude" if self.anthropic_api_key else "mock"


def load_settings(data_dir: Path | None = None, env: dict[str, str] | None = None) -> Settings:
    """With env=None: .env, overridden by the process environment. With an explicit env (tests): only that."""
    merged = dict(load_dotenv(ROOT / ".env")) if env is None else {}
    merged.update(os.environ if env is None else env)

    # Hard refusals: anything that smells like a live/production switch stops the app.
    if merged.get("PAYPAL_MODE", "").lower() in {"live", "production", "prod"}:
        raise ConfigError("PAYPAL_MODE=live is not supported. Countersign only talks to the PayPal sandbox.")
    base = merged.get("PAYPAL_BASE_URL", "")
    if base and base.rstrip("/") != SANDBOX_BASE_URL:
        raise ConfigError(f"PAYPAL_BASE_URL={base!r} refused: only {SANDBOX_BASE_URL} is allowed.")

    return Settings(
        data_dir=Path(data_dir or merged.get("COUNTERSIGN_DATA_DIR") or ROOT / "data"),
        paypal_client_id=merged.get("PAYPAL_CLIENT_ID", ""),
        paypal_client_secret=merged.get("PAYPAL_CLIENT_SECRET", ""),
        anthropic_api_key=merged.get("ANTHROPIC_API_KEY", ""),
        anthropic_model=merged.get("ANTHROPIC_MODEL", "") or "claude-opus-5",
        planner_choice=merged.get("COUNTERSIGN_PLANNER", "").lower(),
        replay_path=Path(merged.get("COUNTERSIGN_REPLAY") or ROOT / "docs" / "claude_run.json"),
        supplier_email=merged.get("COUNTERSIGN_SUPPLIER_EMAIL", "").strip().lower(),
    )
