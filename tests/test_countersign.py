"""Run with: python -m unittest -v"""
from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from countersign import planner as pl
from countersign.config import ConfigError, load_settings
from countersign.engine import DecisionError, Engine
from countersign.money import MoneyError, parse_amount
from countersign.paypal import MockPayPal, SandboxOnlyViolation, SandboxPayPal, assert_sandbox_url


def make_engine(tmp: str, planner=None) -> Engine:
    return Engine(Path(tmp), MockPayPal(Path(tmp) / "mock.json"), planner or pl.MockPlanner())


def by_source(cards, sid):
    return next(c for c in cards if c["source_id"] == sid)


class SandboxOnly(unittest.TestCase):
    def test_live_host_refused(self):
        with self.assertRaises(SandboxOnlyViolation):
            assert_sandbox_url("https://api-m.paypal.com/v2/payments/captures/X")
        with self.assertRaises(SandboxOnlyViolation):
            SandboxPayPal("id", "secret", base_url="https://api-m.paypal.com")

    def test_lookalike_hosts_refused(self):
        for url in ("http://api-m.sandbox.paypal.com/x", "https://api-m.sandbox.paypal.com.evil.example/x",
                    "https://api-m.sandbox.paypal.com:8443/x", "https://user@evil.example/x"):
            with self.assertRaises(SandboxOnlyViolation, msg=url):
                assert_sandbox_url(url)
        assert_sandbox_url("https://api-m.sandbox.paypal.com/v1/oauth2/token")

    def test_config_refuses_live_switches(self):
        with self.assertRaises(ConfigError):
            load_settings(env={"PAYPAL_MODE": "live"})
        with self.assertRaises(ConfigError):
            load_settings(env={"PAYPAL_BASE_URL": "https://api-m.paypal.com"})

    def test_mock_is_default(self):
        s = load_settings(env={})
        self.assertEqual(s.paypal_mode, "mock")
        self.assertEqual(s.planner_mode, "mock")


class Money(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_amount("38"), Decimal("38.00"))
        for bad in (3.5, "-1", "0", "1.234", "NaN", "abc"):
            with self.assertRaises(MoneyError, msg=bad):
                parse_amount(bad)


class DemoFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.eng = make_engine(self.tmp)
        self.cards = self.eng.run_agent()

    def test_expected_cards(self):
        st = {c["source_id"]: c["status"] for c in self.cards}
        self.assertEqual(st, {"msg-1": "pending", "msg-2": "pending", "msg-3": "pending",
                              "msg-4": "blocked", "msg-5": "blocked", "msg-6": "pending"})

    def test_nothing_executes_without_approval(self):
        self.assertFalse([c for c in self.eng.paypal.calls if c[0] == "POST"])

    def test_fraud_payout_blocked_by_code(self):
        c = by_source(self.cards, "msg-4")
        failed = {x["rule"] for x in c["rule_checks"] if not x["ok"]}
        self.assertIn("R6 payee on human-vetted allowlist", failed)
        with self.assertRaises(DecisionError):
            self.eng.approve(c["id"], "alice", c["digest"])

    def test_verification_catches_wrong_claim(self):
        c = by_source(self.cards, "msg-5")
        failed = {x["rule"] for x in c["verification"] if not x["ok"]}
        self.assertIn("V3 claimed captured amount matches PayPal", failed)

    def test_approve_executes_once(self):
        c = by_source(self.cards, "msg-1")
        done = self.eng.approve(c["id"], "alice", c["digest"])
        self.assertEqual(done["status"], "executed")
        self.assertEqual(self.eng.paypal.refunded_total(c["params"]["capture_id"]), Decimal("38.00"))
        with self.assertRaises(DecisionError):
            self.eng.approve(c["id"], "alice", c["digest"])
        self.assertEqual(self.eng.paypal.refunded_total(c["params"]["capture_id"]), Decimal("38.00"))

    def test_tampered_card_refused(self):
        c = by_source(self.cards, "msg-2")
        c["params"]["amount"] = "499.00"  # someone edits the card after it was shown
        with self.assertRaises(DecisionError):
            self.eng.approve(c["id"], "alice", c["digest"])

    def test_stale_digest_refused(self):
        c = by_source(self.cards, "msg-2")
        with self.assertRaises(DecisionError):
            self.eng.approve(c["id"], "alice", "0" * 64)

    def test_agent_cannot_approve(self):
        c = by_source(self.cards, "msg-2")
        for who in ("", "agent", "system"):
            with self.assertRaises(DecisionError):
                self.eng.approve(c["id"], who, c["digest"])

    def test_reject(self):
        c = by_source(self.cards, "msg-3")
        self.assertEqual(self.eng.reject(c["id"], "alice", "wrong price")["status"], "rejected")
        with self.assertRaises(DecisionError):
            self.eng.approve(c["id"], "alice", c["digest"])

    def test_duplicate_blocked(self):
        c = by_source(self.cards, "msg-1")
        self.eng.approve(c["id"], "alice", c["digest"])
        again = self.eng.add_proposal({"source_id": "msg-1", "action": "refund", "params": c["params"],
                                       "claims": c["claims"], "rationale": "again"})
        self.assertEqual(again["status"], "blocked")

    def test_recheck_at_execution_time(self):
        # Two refund cards for the same capture, each fine alone; together they exceed the capture.
        cap = self.eng.lookup_order("A-1044")
        mk = lambda amt, note: self.eng.add_proposal({  # noqa: E731
            "source_id": "msg-x", "action": "refund",
            "params": {"capture_id": cap["capture_id"], "amount": amt, "currency": "USD", "note": note},
            "claims": {"order_ref": "A-1044", "captured_amount": "64.50", "evidence_quote": ""},
            "rationale": "t"})
        self.eng.inbox = self.eng.inbox + [{"id": "msg-x", "from": "x@example.com", "subject": "t", "body": "t"}]
        a, b = mk("40.00", "a"), mk("40.00", "b")
        for card in (a, b):  # evidence quote is empty -> blocked; relax to isolate the re-check
            card["status"] = "pending"
            card["claims"]["evidence_quote"] = "t"
        self.assertEqual(self.eng.approve(a["id"], "alice", a["digest"])["status"], "executed")
        with self.assertRaises(DecisionError):
            self.eng.approve(b["id"], "alice", b["digest"])
        self.assertEqual(self.eng.card(b["id"])["status"], "blocked")

    def test_audit_chain_and_tamper_detection(self):
        c = by_source(self.cards, "msg-1")
        self.eng.approve(c["id"], "alice", c["digest"])
        self.assertTrue(self.eng.audit.verify()[0])
        lines = self.eng.audit.path.read_text().splitlines()
        entry = json.loads(lines[2])
        entry["actor"] = "mallory"
        lines[2] = json.dumps(entry, sort_keys=True)
        self.eng.audit.path.write_text("\n".join(lines) + "\n")
        self.assertFalse(self.eng.audit.verify()[0])

    def test_daily_cap(self):
        self.eng.policy.daily_outflow_cap = Decimal("400.00")
        c = by_source(self.cards, "msg-2")  # 450 payout, created under the old cap
        with self.assertRaises(DecisionError):
            self.eng.approve(c["id"], "alice", c["digest"])


class ClaudeLoop(unittest.TestCase):
    """Exercise the Claude tool-use loop with a fake client (no network, no key)."""

    def test_loop_collects_proposals_and_never_executes(self):
        def tu(i, name, inp):
            return SimpleNamespace(type="tool_use", id=f"t{i}", name=name, input=inp)

        script = [
            SimpleNamespace(stop_reason="tool_use", content=[tu(1, "lookup_order", {"order_ref": "A-1042"})]),
            SimpleNamespace(stop_reason="tool_use", content=[
                tu(2, "propose_refund", {"source_id": "msg-1", "order_ref": "A-1042", "capture_id": "MOCKCAP-1042",
                                         "amount": "38.00", "currency": "USD", "captured_amount": "38.00",
                                         "evidence_quote": "Could you refund the 38.00 USD?", "rationale": "broken"}),
                tu(3, "no_action", {"source_id": "msg-4", "reason": "CEO-fraud pattern"})]),
            SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="done")]),
        ]
        sent = []

        class FakeMessages:
            def create(self, **kw):
                sent.append(kw)
                return script[len(sent) - 1]

        planner = pl.ClaudePlanner.__new__(pl.ClaudePlanner)
        planner.client = SimpleNamespace(messages=FakeMessages())
        planner.model, planner.max_turns, planner.name, planner.notes = "claude-opus-5", 12, "Claude (fake)", []
        tmp = tempfile.mkdtemp()
        planner.record_path, planner.steps = Path(tmp) / "claude_run.json", []
        eng = make_engine(tmp, planner)
        cards = eng.run_agent()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["status"], "pending")
        self.assertEqual(eng.notes[0]["source_id"], "msg-4")
        lookup_result = json.loads(sent[1]["messages"][2]["content"][0]["content"])
        self.assertEqual(lookup_result["captured_amount"], "38.00")
        self.assertFalse([c for c in eng.paypal.calls if c[0] == "POST"])
        self.assertTrue(all(t["name"] != "execute" for t in sent[0]["tools"]))
        self.assertIn("propose_purchase", {t["name"] for t in sent[0]["tools"]})

        # The run was recorded; replaying it offline yields the same card and note, with no model call.
        rec = json.loads(planner.record_path.read_text())
        self.assertEqual([s["tool"] for s in rec["steps"]], ["lookup_order", "propose_refund", "no_action"])
        self.assertNotIn("api_key", planner.record_path.read_text().lower())
        replay = pl.ReplayPlanner(planner.record_path)
        eng2 = make_engine(tempfile.mkdtemp(), replay)
        cards2 = eng2.run_agent()
        self.assertEqual([(c["action"], c["amount"], c["status"]) for c in cards2], [("refund", "38.00", "pending")])
        self.assertEqual(eng2.notes[0]["reason"], "CEO-fraud pattern")
        self.assertIn("replay", eng2.planner.name)
        self.assertEqual(len(sent), 3)  # replay made no further model calls

    def test_replay_still_enforces_rules(self):
        """A recording edited to inflate a refund is still blocked: replay goes through the same checks."""
        tmp = Path(tempfile.mkdtemp())
        (tmp / "run.json").write_text(json.dumps({"model": "m", "recorded_at": "2026-10-24T00:00:00", "steps": [
            {"tool": "propose_refund", "input": {
                "source_id": "msg-1", "order_ref": "A-1042", "capture_id": "MOCKCAP-1042", "amount": "380.00",
                "currency": "USD", "captured_amount": "380.00", "evidence_quote": "Could you refund the 38.00 USD?",
                "rationale": "edited"}}]}))
        eng = make_engine(str(tmp), pl.ReplayPlanner(tmp / "run.json"))
        self.assertEqual(eng.run_agent()[0]["status"], "blocked")

    def test_planner_selection(self):
        self.assertEqual(load_settings(env={"COUNTERSIGN_PLANNER": "replay"}).planner_mode, "replay")
        self.assertEqual(load_settings(env={"ANTHROPIC_API_KEY": "x", "COUNTERSIGN_PLANNER": "mock"}).planner_mode,
                         "mock")
        s = load_settings(env={"COUNTERSIGN_PLANNER": "replay", "COUNTERSIGN_REPLAY": "/nonexistent/run.json"})
        self.assertIsInstance(pl.make_planner(s), pl.MockPlanner)  # falls back when no recording exists


class AgenticCommerce(unittest.TestCase):
    """The agent proposes a supplier purchase (PayPal Orders v2); the owner approves, then pays at PayPal."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.eng = make_engine(self.tmp)
        self.card = by_source(self.eng.run_agent(), "msg-6")

    def proposal(self, **over):
        item = {"sku": "BLANK-ESP-6", "name": "Espresso cup blanks, box of 6", "quantity": 5, "unit_price": "21.00"}
        item.update(over.pop("item", {}))
        params = {"supplier": "sales@clayworks-supply.example", "currency": "USD", "items": [item], "note": "t"}
        params.update(over)
        return {"source_id": "msg-6", "action": "purchase", "params": params,
                "claims": {"evidence_quote": "Suggested reorder: 5 boxes of BLANK-ESP-6 from the usual supplier."},
                "rationale": "t"}

    def failed(self, card):
        return {x["rule"] for x in card["rule_checks"] + card["verification"] if not x["ok"]}

    def test_purchase_card_from_stock_alert(self):
        c = self.card
        self.assertEqual((c["action"], c["status"], c["amount"]), ("purchase", "pending", "105.00"))
        self.assertFalse(self.failed(c))
        self.assertFalse([x for x in self.eng.paypal.calls if x[0] == "POST"])

    def test_approve_creates_order_then_owner_pays(self):
        done = self.eng.approve(self.card["id"], "alice", self.card["digest"])
        self.assertEqual(done["status"], "executed")
        self.assertEqual(done["result"]["status"], "PAYER_ACTION_REQUIRED")
        self.assertTrue(done["result"]["approve_url"])
        order = self.eng.paypal.state["orders"][done["result"]["id"]]
        self.assertEqual(order["purchase_units"][0]["payee"]["email_address"], "sales@clayworks-supply.example")
        self.assertEqual(order["purchase_units"][0]["amount"]["value"], "105.00")
        paid = self.eng.complete_purchase(self.card["id"], "alice")
        self.assertEqual(paid["result"]["status"], "COMPLETED")
        self.assertTrue(paid["result"]["capture_id"])
        with self.assertRaises(DecisionError):
            self.eng.complete_purchase(self.card["id"], "alice")  # cannot pay twice
        events = [a["event"] for a in self.eng.audit.entries()]
        self.assertIn("payer_approval_simulated", events)
        self.assertIn("purchase_paid", events)
        self.assertTrue(self.eng.audit.verify()[0])

    def test_purchase_counts_toward_daily_cap(self):
        self.eng.approve(self.card["id"], "alice", self.card["digest"])
        self.assertEqual(self.eng._outflow_today(), Decimal("105.00"))

    def test_cannot_complete_unapproved_or_other_cards(self):
        with self.assertRaises(DecisionError):
            self.eng.complete_purchase(self.card["id"], "alice")  # still pending
        self.eng.approve(self.card["id"], "alice", self.card["digest"])
        with self.assertRaises(DecisionError):
            self.eng.complete_purchase(self.card["id"], "agent")

    def test_sandbox_capture_refused_until_payer_approves(self):
        """Mock mirrors PayPal: capture before payer approval fails with ORDER_NOT_APPROVED."""
        from countersign.paypal import PayPalError
        done = self.eng.approve(self.card["id"], "alice", self.card["digest"])
        with self.assertRaises(PayPalError) as ctx:
            self.eng.paypal.capture_order(done["result"]["id"], "x")
        self.assertEqual(ctx.exception.name, "ORDER_NOT_APPROVED")

    def test_inflated_price_blocked(self):
        c = self.eng.add_proposal(self.proposal(item={"unit_price": "49.00"}))
        self.assertIn("R10 vetted supplier, catalogue price and quantity", self.failed(c))

    def test_unknown_supplier_or_sku_blocked(self):
        c = self.eng.add_proposal(self.proposal(supplier="quickpay.supplier@example.net"))
        self.assertIn("R10 vetted supplier, catalogue price and quantity", self.failed(c))
        c = self.eng.add_proposal(self.proposal(item={"sku": "GOLD-BAR-1"}))
        self.assertTrue({"R10 vetted supplier, catalogue price and quantity",
                         "V6 every SKU bought is named in the source message"} <= self.failed(c))

    def test_quantity_and_limit(self):
        c = self.eng.add_proposal(self.proposal(item={"quantity": 15}))
        self.assertTrue({"R4 per-action limit", "R10 vetted supplier, catalogue price and quantity"} <= self.failed(c))

    def test_sandbox_order_body_shape(self):
        from countersign.paypal import purchase_order_body
        b = purchase_order_body("s@example.com", [{"sku": "BLANK-ESP-6", "name": "n", "quantity": 5,
                                                   "unit_price": "21.00"}], "USD", "note", "card-1", "http://x/")
        pu = b["purchase_units"][0]
        self.assertEqual(b["intent"], "CAPTURE")
        self.assertEqual(pu["amount"]["breakdown"]["item_total"]["value"], "105.00")
        self.assertEqual(pu["items"][0]["quantity"], "5")
        self.assertEqual(pu["payee"]["email_address"], "s@example.com")


class WebRender(unittest.TestCase):
    def test_page_explains_blocks_and_offers_purchase_payment(self):
        from countersign.web import render
        eng = make_engine(tempfile.mkdtemp())
        cards = eng.run_agent()
        buy = by_source(cards, "msg-6")
        eng.approve(buy["id"], "alice", buy["digest"])
        page = render(eng.state(), "tok")
        self.assertIn("Blocked by code because", page)
        self.assertIn("the payee is not on the human-vetted list", page)
        self.assertIn('action="/pay"', page)
        self.assertIn("agentic commerce", page)
        self.assertEqual(page.count("Approve (blocked)"), 2)
        self.assertNotIn("<script", page)


class SandboxFixes(unittest.TestCase):
    """Fixes found by running against the real PayPal sandbox on 2026-11-07."""

    class FakeSandbox:
        """Sandbox-shaped captures: the order ref is in custom_id, invoice_id is null (as PayPal returns them)."""
        mode = "sandbox"

        def __init__(self):
            self.caps = {"9S43TEST": {"id": "9S43TEST", "status": "COMPLETED", "invoice_id": None, "custom_id": "A-1042",
                                      "amount": {"currency_code": "USD", "value": "38.00"}}}
            self.refunds = []

        def get_capture(self, capture_id):
            from countersign.paypal import PayPalError
            if capture_id not in self.caps:
                raise PayPalError(404, "RESOURCE_NOT_FOUND", "no capture")
            return self.caps[capture_id]

        def refund_capture(self, capture_id, amount, currency, note, request_id):
            self.refunds.append((capture_id, amount, request_id))
            return {"id": "REF1", "status": "COMPLETED"}

    def test_explicit_env_ignores_dotenv(self):
        # A developer .env with sandbox keys must not switch the test suite to the real sandbox.
        self.assertEqual(load_settings(env={}).paypal_mode, "mock")
        self.assertEqual(load_settings(env={"COUNTERSIGN_SUPPLIER_EMAIL": " SB-Shop@Business.Example.com "})
                         .supplier_email, "sb-shop@business.example.com")

    def test_seed_body_uses_custom_id_not_invoice_id(self):
        # invoice_id must be unique per PayPal account forever: a re-seed failed with DUPLICATE_INVOICE_ID.
        from countersign.paypal import seed_order_body
        b = seed_order_body("A-1042", "38.00", "USD")
        pu = b["purchase_units"][0]
        self.assertEqual(pu["custom_id"], "A-1042")
        self.assertNotIn("invoice_id", pu)
        self.assertNotIn("payment_source", b)
        card = seed_order_body("A-1042", "38.00", "USD", card=True)["payment_source"]["card"]
        self.assertEqual(card["number"], "4005519200000004")  # 4111... is declined by the sandbox (422)

    def test_order_ref_prefers_custom_id(self):
        from countersign.paypal import order_ref_of
        self.assertEqual(order_ref_of({"custom_id": "A-1", "invoice_id": None}), "A-1")
        self.assertEqual(order_ref_of({"invoice_id": "A-2"}), "A-2")  # mock captures

    def test_sandbox_refund_path_verifies_against_custom_id(self):
        tmp = tempfile.mkdtemp()
        pp = self.FakeSandbox()
        (Path(tmp) / "sandbox_orders.json").write_text(json.dumps({"A-1042": "9S43TEST"}))
        eng = Engine(Path(tmp), pp, pl.MockPlanner(), inbox=[m for m in pl_inbox() if m["id"] == "msg-1"])
        card = eng.run_agent()[0]
        self.assertEqual((card["action"], card["status"]), ("refund", "pending"), card["verification"])
        done = eng.approve(card["id"], "alice", card["digest"])
        self.assertEqual(done["status"], "executed")
        self.assertEqual(pp.refunds, [("9S43TEST", Decimal("38.00"), card["id"])])
        pp.caps["9S43TEST"]["custom_id"] = "A-9999"  # capture belongs to another order -> V2 fails
        c2 = Engine(Path(tempfile.mkdtemp()), pp, pl.MockPlanner()).add_proposal(
            {"source_id": "msg-1", "action": "refund", "params": {"capture_id": "9S43TEST", "amount": "1.00",
             "currency": "USD"}, "claims": {"order_ref": "A-1042", "evidence_quote": "Could you refund the 38.00 USD?"}})
        self.assertEqual(c2["status"], "blocked")

    def test_supplier_override_for_sandbox_business_account(self):
        from countersign import policy as pol
        eng = Engine(Path(tempfile.mkdtemp()), MockPayPal(), pl.MockPlanner(),
                     policy=pol.Policy().with_supplier("SB-Supplier@business.example.com"))
        buy = by_source(eng.run_agent(), "msg-6")
        self.assertEqual(buy["status"], "pending")
        self.assertEqual(buy["params"]["supplier"], "sb-supplier@business.example.com")

    def test_demo_uses_one_currency(self):
        # Sandbox business accounts hold USD: EUR payouts failed with INSUFFICIENT_FUNDS. One currency also keeps
        # the daily outflow cap (a plain sum) meaningful.
        from countersign import policy as pol
        from countersign.paypal import DEMO_CAPTURES
        curs = {c["currency_code"] for c in DEMO_CAPTURES.values()}
        curs |= {v["currency"] for v in pol.Policy().supplier_catalog.values()}
        curs |= {cur for m in pl_inbox() for _, cur in pl.AMOUNT_RE.findall(m["body"])}
        self.assertEqual(curs, {"USD"})


def pl_inbox():
    from countersign.inbox import DEMO_INBOX
    return DEMO_INBOX


if __name__ == "__main__":
    unittest.main()
