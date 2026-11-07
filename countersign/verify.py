"""Verification: check what the agent *claims* against what PayPal and the source message say.

The agent writes claims on every proposal (which order, what was captured, a verbatim quote
from the message that justifies the amount). None of these is trusted. A failed check blocks the card.
"""
from __future__ import annotations

from decimal import Decimal

from .money import MoneyError, parse_amount
from .paypal import order_ref_of
from .policy import Check


def _norm(s: str) -> str:
    return " ".join(s.split()).lower()


def verify(proposal: dict, source_text: str, facts: dict) -> list[Check]:
    checks: list[Check] = []
    claims = proposal.get("claims", {})
    p = proposal.get("params", {})

    quote = (claims.get("evidence_quote") or "").strip()
    grounded = bool(quote) and _norm(quote) in _norm(source_text)
    checks.append(Check("V1 evidence quote appears in the source message", grounded,
                        f'"{quote}"' if quote else "no quote given"))

    if proposal.get("action") == "refund":
        cap = facts.get("capture")
        ref = claims.get("order_ref")
        if cap is None:
            checks.append(Check("V2 order exists at PayPal", False, f"no capture found for order {ref!r}"))
        else:
            at_paypal = order_ref_of(cap)
            checks.append(Check("V2 capture belongs to the claimed order", at_paypal == ref,
                                f"PayPal capture {cap['id']} is for order {at_paypal!r}, agent said {ref!r}"))
            claimed = claims.get("captured_amount")
            if claimed is not None:
                try:
                    match = parse_amount(claimed) == Decimal(cap["amount"]["value"])
                except MoneyError:
                    match = False
                checks.append(Check("V3 claimed captured amount matches PayPal", match,
                                    f"agent: {claimed}, PayPal: {cap['amount']['value']} {cap['amount']['currency_code']}"))
            if p.get("capture_id") and p["capture_id"] != cap["id"]:
                checks.append(Check("V4 capture id consistent", False, f"{p['capture_id']} vs {cap['id']}"))

    if proposal.get("action") in ("payout", "invoice"):
        who = p.get("receiver") or p.get("recipient") or ""
        checks.append(Check("V5 counterparty email appears in the source message",
                            bool(who) and who.lower() in source_text.lower(), who or "(none)"))

    if proposal.get("action") == "purchase":
        skus = [str(it.get("sku") or "") for it in p.get("items") or []]
        missing = [s for s in skus if not s or s.lower() not in source_text.lower()]
        checks.append(Check("V6 every SKU bought is named in the source message", bool(skus) and not missing,
                            ", ".join(skus) + (f" (not in message: {', '.join(missing)})" if missing else "")))
    return checks
