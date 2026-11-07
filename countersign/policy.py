"""Boundary rules, enforced in code. The model never sees a way around them.

The agent can only *propose*. Every proposal is checked against these rules twice:
once when the decision card is created, and again right before execution (the facts may
have changed in between, e.g. another refund landed). A rule failure blocks the card;
the human cannot approve a blocked card.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from .money import MoneyError, parse_amount

ACTIONS = {"refund", "payout", "invoice", "purchase"}
OUTFLOW_ACTIONS = {"refund", "payout", "purchase"}
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


@dataclass
class Policy:
    currencies: frozenset = frozenset({"EUR", "USD"})
    max_per_action: dict = field(default_factory=lambda: {
        "refund": Decimal("200.00"), "payout": Decimal("500.00"), "invoice": Decimal("5000.00"),
        "purchase": Decimal("300.00")})
    daily_outflow_cap: Decimal = Decimal("1000.00")
    # Payees a human has vetted. The agent cannot add to this list.
    payee_allowlist: dict = field(default_factory=lambda: {
        "ana.photo@example.com": "Ana Ribeiro (product photographer)",
        "packaging@example.org": "Kartonfabriek (packaging supplier)",
    })
    # Supplier catalogue a human has vetted: what the agent may buy, from whom, at what price.
    # The agent cannot add suppliers or change prices. In sandbox mode, set the supplier email to one of
    # your sandbox *business* accounts (PayPal must know the payee).
    supplier_catalog: dict = field(default_factory=lambda: {
        "BLANK-ESP-6": {"supplier": "sales@clayworks-supply.example", "supplier_name": "Clayworks Supply",
                        "name": "Espresso cup blanks, box of 6", "unit_price": Decimal("21.00"),
                        "currency": "USD", "max_qty": 10},
        "GLAZE-CEL-1L": {"supplier": "sales@clayworks-supply.example", "supplier_name": "Clayworks Supply",
                         "name": "Celadon glaze, 1 litre", "unit_price": Decimal("16.50"),
                         "currency": "USD", "max_qty": 6},
    })
    max_invoice_lines: int = 20
    card_ttl_hours: int = 24

    def with_supplier(self, email: str) -> "Policy":
        """Point every catalogue line at one supplier address (sandbox: a sandbox business account)."""
        if email:
            self.supplier_catalog = {sku: {**v, "supplier": email.lower()} for sku, v in self.supplier_catalog.items()}
        return self


@dataclass
class Check:
    rule: str
    ok: bool
    detail: str

    def as_dict(self) -> dict:
        return {"rule": self.rule, "ok": self.ok, "detail": self.detail}


def evaluate(proposal: dict, facts: dict, policy: Policy) -> list[Check]:
    """Return one Check per rule. `facts` come from PayPal and the local ledger, never from the model.

    facts keys: capture (dict|None), refunded_total (Decimal), outflow_today (Decimal),
                duplicate_of (card id|None)
    """
    checks: list[Check] = []
    action = proposal.get("action")
    p = proposal.get("params", {})

    checks.append(Check("R1 known action", action in ACTIONS, f"action={action!r}"))
    if action not in ACTIONS:
        return checks

    # R2 amount is exact and positive
    try:
        if action in ("invoice", "purchase"):
            lines = p.get("items") or []
            if not lines or len(lines) > policy.max_invoice_lines:
                raise MoneyError(f"invoice needs 1..{policy.max_invoice_lines} lines")
            amount = sum((parse_amount(i["unit_price"]) * int(i["quantity"]) for i in lines), Decimal("0"))
            if any(int(i["quantity"]) <= 0 for i in lines):
                raise MoneyError("quantities must be positive")
        else:
            amount = parse_amount(p.get("amount"))
        checks.append(Check("R2 valid amount", True, f"{amount} {p.get('currency')}"))
    except (MoneyError, KeyError, TypeError, ValueError) as exc:
        checks.append(Check("R2 valid amount", False, str(exc)))
        return checks

    cur = p.get("currency")
    checks.append(Check("R3 allowed currency", cur in policy.currencies, f"{cur} in {sorted(policy.currencies)}"))

    limit = policy.max_per_action[action]
    checks.append(Check("R4 per-action limit", amount <= limit, f"{amount} <= {limit}"))

    if action in OUTFLOW_ACTIONS:
        after = facts.get("outflow_today", Decimal("0")) + amount
        checks.append(Check("R5 daily outflow cap", after <= policy.daily_outflow_cap,
                            f"today incl. this: {after} <= {policy.daily_outflow_cap}"))

    if action == "payout":
        rcv = (p.get("receiver") or "").lower()
        ok = rcv in policy.payee_allowlist
        checks.append(Check("R6 payee on human-vetted allowlist", ok,
                            policy.payee_allowlist.get(rcv, f"{rcv or '(none)'} is NOT on the allowlist")))

    if action == "refund":
        cap = facts.get("capture")
        if not cap:
            checks.append(Check("R7 refund within captured amount", False, "capture not found at PayPal"))
        else:
            captured = Decimal(cap["amount"]["value"])
            remaining = captured - facts.get("refunded_total", Decimal("0"))
            same_cur = cap["amount"]["currency_code"] == cur
            ok = same_cur and amount <= remaining and cap.get("status") in {"COMPLETED", "PARTIALLY_REFUNDED"}
            checks.append(Check("R7 refund within captured amount", ok,
                                f"{amount} {cur} <= refundable {remaining} {cap['amount']['currency_code']}"
                                f" (capture status {cap.get('status')})"))

    if action == "invoice":
        rcp = p.get("recipient") or ""
        checks.append(Check("R8 invoice recipient is an email", bool(EMAIL_RE.match(rcp)), rcp or "(none)"))

    if action == "purchase":
        sup = (p.get("supplier") or "").lower()
        bad: list[str] = []
        for it in p.get("items") or []:
            entry = policy.supplier_catalog.get(it.get("sku"))
            if not entry:
                bad.append(f"{it.get('sku')!r} not in vetted catalogue")
                continue
            if entry["supplier"] != sup:
                bad.append(f"{it['sku']} is sold by {entry['supplier']}, not {sup or '(none)'}")
            if parse_amount(it["unit_price"]) != entry["unit_price"] or cur != entry["currency"]:
                bad.append(f"{it['sku']} price {it['unit_price']} {cur} != catalogue {entry['unit_price']} "
                           f"{entry['currency']}")
            if int(it["quantity"]) > entry["max_qty"]:
                bad.append(f"{it['sku']} quantity {it['quantity']} > max {entry['max_qty']}")
        checks.append(Check("R10 vetted supplier, catalogue price and quantity", not bad,
                            "; ".join(bad) or f"all lines match the human-vetted catalogue of {sup}"))

    dup = facts.get("duplicate_of")
    checks.append(Check("R9 no duplicate money action", dup is None,
                        f"same action already proposed in card {dup}" if dup else "first of its kind"))
    return checks


def proposal_amount(proposal: dict) -> Decimal:
    p = proposal["params"]
    if proposal["action"] in ("invoice", "purchase"):
        return sum((parse_amount(i["unit_price"]) * int(i["quantity"]) for i in p["items"]), Decimal("0"))
    return parse_amount(p["amount"])
