"""Orchestration: proposals -> decision cards -> human decision -> guarded execution.

Card lifecycle:
    pending  --approve-->  executed | failed
        (a purchase "executes" by creating a PayPal order; the owner then pays it at PayPal, and
         complete_purchase() captures it: payment.status goes awaiting_payer -> paid)
    pending  --reject--->  rejected
    blocked  (a rule or verification failed; it cannot be approved)
    pending  --ttl------>  expired
"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from . import policy as pol
from .audit import AuditLog
from .inbox import DEMO_INBOX
from .money import MoneyError, fmt
from .paypal import MockPayPal, PayPalError, order_ref_of, summarize_order
from .planner import ReadTools
from .verify import verify

ACTIVE = {"pending", "executed"}


def now() -> datetime:
    return datetime.now(timezone.utc)


def digest(action: str, params: dict) -> str:
    return hashlib.sha256(json.dumps({"action": action, "params": params}, sort_keys=True).encode()).hexdigest()


class DecisionError(RuntimeError):
    pass


class Engine:
    def __init__(self, data_dir: Path, paypal, planner, policy: pol.Policy | None = None,
                 inbox: list[dict] | None = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.paypal = paypal
        self.planner = planner
        self.policy = policy or pol.Policy()
        self.inbox = inbox if inbox is not None else DEMO_INBOX
        self.audit = AuditLog(self.data_dir / "audit.jsonl")
        self.cards_path = self.data_dir / "cards.json"
        self.cards: list[dict] = json.loads(self.cards_path.read_text()) if self.cards_path.exists() else []
        self.sandbox_orders_path = self.data_dir / "sandbox_orders.json"
        self.notes: list[dict] = []
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- persistence
    def _save(self) -> None:
        tmp = self.cards_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cards, indent=1))
        tmp.replace(self.cards_path)

    def card(self, card_id: str) -> dict:
        for c in self.cards:
            if c["id"] == card_id:
                return c
        raise DecisionError(f"no card {card_id}")

    # ---------------------------------------------------------------- facts (never from the model)
    def _capture_for(self, capture_id: str | None, order_ref: str | None) -> dict | None:
        cid = capture_id
        if not cid and order_ref:
            cid = self._capture_id_for_order(order_ref)
        if not cid:
            return None
        try:
            return self.paypal.get_capture(cid)
        except PayPalError:
            return None

    def _capture_id_for_order(self, order_ref: str) -> str | None:
        if isinstance(self.paypal, MockPayPal):
            for cid, cap in self.paypal.state["captures"].items():
                if cap.get("invoice_id") == order_ref:
                    return cid
            return None
        if self.sandbox_orders_path.exists():
            return json.loads(self.sandbox_orders_path.read_text()).get(order_ref)
        return None

    def lookup_order(self, order_ref: str) -> dict | None:
        cap = self._capture_for(None, order_ref)
        if not cap:
            return None
        return {"capture_id": cap["id"], "status": cap.get("status"), "captured_amount": cap["amount"]["value"],
                "currency": cap["amount"]["currency_code"], "order_ref": order_ref_of(cap)}

    def supplier_catalog(self) -> dict:
        """Read-only view of the human-vetted catalogue for the planner (prices as strings)."""
        return {sku: {**v, "unit_price": fmt(v["unit_price"])} for sku, v in self.policy.supplier_catalog.items()}

    def _refunded_total(self, capture_id: str, exclude: str | None = None) -> Decimal:
        if isinstance(self.paypal, MockPayPal):
            return self.paypal.refunded_total(capture_id)
        return sum((Decimal(c["amount"]) for c in self.cards if c["action"] == "refund" and c["status"] == "executed"
                    and c["params"].get("capture_id") == capture_id and c["id"] != exclude), Decimal("0"))

    def _outflow_today(self, exclude: str | None = None) -> Decimal:
        today = now().date().isoformat()
        return sum((Decimal(c["amount"]) for c in self.cards
                    if c["action"] in pol.OUTFLOW_ACTIONS and c["status"] == "executed"
                    and (c.get("decided_at") or "").startswith(today) and c["id"] != exclude), Decimal("0"))

    def _facts(self, proposal: dict, exclude: str | None = None) -> dict:
        p = proposal.get("params", {})
        facts: dict = {"outflow_today": self._outflow_today(exclude), "capture": None,
                       "refunded_total": Decimal("0"), "duplicate_of": None}
        if proposal.get("action") == "refund":
            cap = self._capture_for(p.get("capture_id"), proposal.get("claims", {}).get("order_ref"))
            facts["capture"] = cap
            if cap:
                facts["refunded_total"] = self._refunded_total(cap["id"], exclude)
        d = digest(proposal.get("action", ""), p)
        for c in self.cards:
            if c["id"] != exclude and c["digest"] == d and c["status"] in ACTIVE:
                facts["duplicate_of"] = c["id"]
        return facts

    def _source(self, source_id: str) -> dict:
        return next((m for m in self.inbox if m["id"] == source_id), {"id": source_id, "subject": "?", "body": ""})

    # ---------------------------------------------------------------- agent run
    def run_agent(self) -> list[dict]:
        with self._lock:
            self.audit.record("agent_run_started", "agent", planner=self.planner.name,
                              paypal_mode=self.paypal.mode, messages=len(self.inbox))
            tools = ReadTools(self.lookup_order, lambda: dict(self.policy.payee_allowlist), self.supplier_catalog)
            proposals = self.planner.plan(self.inbox, tools)
            self.notes = list(getattr(self.planner, "notes", []))
            created = [self.add_proposal(p) for p in proposals]
            for n in self.notes:
                self.audit.record("agent_no_action", "agent", **n)
            self.audit.record("agent_run_finished", "agent", proposals=len(created),
                              blocked=sum(c["status"] == "blocked" for c in created))
            return created

    def add_proposal(self, proposal: dict) -> dict:
        with self._lock:
            src = self._source(proposal.get("source_id", ""))
            facts = self._facts(proposal)
            checks = [c.as_dict() for c in pol.evaluate(proposal, facts, self.policy)]
            vchecks = [c.as_dict() for c in verify(proposal, src.get("body", ""), facts)]
            ok = all(c["ok"] for c in checks + vchecks)
            try:
                amount = fmt(pol.proposal_amount(proposal))
            except (MoneyError, KeyError, TypeError, ValueError):
                amount = "0.00"
            card = {
                "id": "card-" + uuid.uuid4().hex[:8],
                "created_at": now().isoformat(timespec="seconds"),
                "expires_at": (now() + timedelta(hours=self.policy.card_ttl_hours)).isoformat(timespec="seconds"),
                "source_id": src["id"], "source_subject": src.get("subject", ""), "source_from": src.get("from", ""),
                "action": proposal.get("action"), "params": proposal.get("params", {}),
                "claims": proposal.get("claims", {}), "rationale": proposal.get("rationale", ""),
                "planner": self.planner.name, "amount": amount, "currency": proposal.get("params", {}).get("currency"),
                "rule_checks": checks, "verification": vchecks,
                "digest": digest(proposal.get("action", ""), proposal.get("params", {})),
                "status": "pending" if ok else "blocked",
                "decided_by": None, "decided_at": None, "decision_note": None, "result": None, "error": None,
            }
            self.cards.append(card)
            self._save()
            failed = [c["rule"] for c in checks + vchecks if not c["ok"]]
            self.audit.record("card_created", "agent", card=card["id"], action=card["action"], amount=amount,
                              currency=card["currency"], status=card["status"], failed_checks=failed,
                              digest=card["digest"])
            return card

    # ---------------------------------------------------------------- human decisions
    def _expire(self) -> None:
        t = now().isoformat(timespec="seconds")
        for c in self.cards:
            if c["status"] == "pending" and c["expires_at"] < t:
                c["status"] = "expired"
                self.audit.record("card_expired", "system", card=c["id"])

    def reject(self, card_id: str, who: str, note: str = "") -> dict:
        with self._lock:
            self._expire()
            c = self.card(card_id)
            if c["status"] not in ("pending", "blocked"):
                raise DecisionError(f"card is {c['status']}, cannot reject")
            c.update(status="rejected", decided_by=who, decided_at=now().isoformat(timespec="seconds"),
                     decision_note=note)
            self._save()
            self.audit.record("card_rejected", who, card=card_id, note=note)
            return c

    def approve(self, card_id: str, who: str, shown_digest: str, note: str = "") -> dict:
        """Approve and execute. `shown_digest` is the digest the human saw; it must still match."""
        with self._lock:
            self._expire()
            c = self.card(card_id)
            if not who or who.strip().lower() in {"agent", "system"}:
                raise DecisionError("approver must be a named human")
            if c["status"] != "pending":
                raise DecisionError(f"card is {c['status']}, cannot approve")
            if shown_digest != c["digest"] or digest(c["action"], c["params"]) != c["digest"]:
                self.audit.record("approval_refused", who, card=card_id, reason="digest mismatch")
                raise DecisionError("card content changed since it was shown; refusing")

            # Re-check every rule and every claim against fresh PayPal facts (time-of-use check).
            proposal = {"action": c["action"], "params": c["params"], "claims": c["claims"]}
            facts = self._facts(proposal, exclude=c["id"])
            checks = [x.as_dict() for x in pol.evaluate(proposal, facts, self.policy)]
            vchecks = [x.as_dict() for x in verify(proposal, self._source(c["source_id"]).get("body", ""), facts)]
            failed = [x["rule"] for x in checks + vchecks if not x["ok"]]
            if failed:
                c.update(status="blocked", rule_checks=checks, verification=vchecks)
                self._save()
                self.audit.record("approval_refused", who, card=card_id, reason="re-check failed", failed=failed)
                raise DecisionError("re-check at execution time failed: " + ", ".join(failed))

            c.update(decided_by=who, decided_at=now().isoformat(timespec="seconds"), decision_note=note)
            self.audit.record("card_approved", who, card=card_id, digest=c["digest"], note=note)
            return self._execute(c)

    def _execute(self, c: dict) -> dict:
        p, rid = c["params"], c["id"]  # card id doubles as PayPal-Request-Id (idempotency key)
        try:
            amt = pol.proposal_amount(c)
            if c["action"] == "refund":
                res = self.paypal.refund_capture(p["capture_id"], amt, p["currency"], p.get("note", ""), rid)
            elif c["action"] == "payout":
                res = self.paypal.create_payout(p["receiver"], amt, p["currency"], p.get("note", ""), rid)
            elif c["action"] == "invoice":
                res = self.paypal.create_and_send_invoice(p["recipient"], p["items"], p["currency"],
                                                          p.get("note", ""), rid)
            elif c["action"] == "purchase":
                res = self.paypal.create_purchase_order(p["supplier"], p["items"], p["currency"],
                                                        p.get("note", ""), rid)
            else:  # unreachable: R1 blocks unknown actions
                raise DecisionError("unknown action")
            c.update(status="executed", result=res)
            self.audit.record("executed", "executor", card=rid, paypal_mode=self.paypal.mode,
                              paypal_id=res.get("id") or res.get("batch_header", {}).get("payout_batch_id"),
                              paypal_status=res.get("status") or res.get("batch_header", {}).get("batch_status"))
        except PayPalError as err:
            c.update(status="failed", error=str(err))
            self.audit.record("execution_failed", "executor", card=rid, error=str(err), debug_id=err.debug_id)
        self._save()
        return c

    def complete_purchase(self, card_id: str, who: str) -> dict:
        """Capture an approved purchase order once the owner has paid it at PayPal.

        In mock mode there is no PayPal page to log in to, so the payer approval is simulated and
        recorded in the audit log as simulated. In sandbox mode PayPal refuses the capture
        (ORDER_NOT_APPROVED) until the owner has really approved it.
        """
        with self._lock:
            c = self.card(card_id)
            if c["action"] != "purchase" or c["status"] != "executed":
                raise DecisionError("only an executed purchase card can be completed")
            if not who or who.strip().lower() in {"agent", "system"}:
                raise DecisionError("approver must be a named human")
            order_id = (c.get("result") or {}).get("id")
            if (c.get("result") or {}).get("status") == "COMPLETED":
                raise DecisionError("purchase already paid")
            try:
                if isinstance(self.paypal, MockPayPal):
                    self.paypal.simulate_payer_approval(order_id)
                    self.audit.record("payer_approval_simulated", who, card=card_id, order=order_id,
                                      note="mock mode: stands in for paying at PayPal")
                res = self.paypal.capture_order(order_id, c["id"] + "-capture")
                if "purchase_units" in res:
                    res = summarize_order(res)
            except PayPalError as err:
                self.audit.record("purchase_capture_failed", who, card=card_id, error=str(err), debug_id=err.debug_id)
                raise DecisionError(f"PayPal: {err}") from None
            c["result"] = {**c["result"], **res, "approve_url": c["result"].get("approve_url")}
            self._save()
            self.audit.record("purchase_paid", who, card=card_id, order=order_id, capture=res.get("capture_id"))
            return c

    # ---------------------------------------------------------------- views
    def state(self) -> dict:
        with self._lock:
            self._expire()
            ok, msg = self.audit.verify()
            return {"paypal_mode": self.paypal.mode, "planner": self.planner.name, "inbox": self.inbox,
                    "cards": self.cards, "notes": self.notes, "audit": self.audit.entries()[-40:],
                    "audit_ok": ok, "audit_msg": msg,
                    "outflow_today": fmt(self._outflow_today()) if self._outflow_today() else "0.00",
                    "policy": {"max_per_action": {k: fmt(v) for k, v in self.policy.max_per_action.items()},
                               "daily_outflow_cap": fmt(self.policy.daily_outflow_cap),
                               "payees": self.policy.payee_allowlist,
                               "catalog": self.supplier_catalog(),
                               "currencies": sorted(self.policy.currencies)}}

    def reset(self) -> None:
        with self._lock:
            self.cards = []
            self.notes = []
            self._save()
            if isinstance(self.paypal, MockPayPal):
                self.paypal.reset()
            self.audit.record("demo_reset", "human")
