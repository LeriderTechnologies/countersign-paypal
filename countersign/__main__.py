"""CLI: python -m countersign {serve|demo|verify-audit|sandbox-seed|sandbox-capture}"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

from .config import ConfigError, load_settings
from .engine import DecisionError, Engine
from .web import REASON
from .paypal import DEMO_CAPTURES, SandboxPayPal, make_paypal, seed_order_body
from .planner import make_planner
from .policy import Policy


def build(settings) -> Engine:
    return Engine(settings.data_dir, make_paypal(settings), make_planner(settings),
                  policy=Policy().with_supplier(settings.supplier_email))


def _why(card: dict) -> str:
    failed = [x["rule"].split(" ", 1)[0] for x in card["verification"] + card["rule_checks"] if not x["ok"]]
    return "; ".join(dict.fromkeys(REASON.get(f, f) for f in failed))


def cmd_demo(engine: Engine) -> int:
    """Headless end-to-end run. A scripted reviewer stands in for the human, and is labelled as such."""
    engine.reset()
    print(f"Countersign demo | PayPal backend: {engine.paypal.mode} | agent: {engine.planner.name}\n")
    print(f"1. The agent reads {len(engine.inbox)} inbox messages and proposes money actions. It cannot execute.")
    cards = engine.run_agent()
    for c in cards:
        line = f"   [{c['status'].upper():7}] {c['action']:8} {c['amount']:>8} {c['currency']}  <- {c['source_id']} {c['source_subject']!r}"
        print(line + (f"\n             blocked because {_why(c)}" if c["status"] == "blocked" else ""))
    for n in engine.notes:
        print(f"   [NO ACTION] {n.get('source_id')}: {n.get('reason')}")
    print("\n2. A human signs the pending cards (here a scripted reviewer; in the web UI, you):")
    for c in cards:
        try:
            if c["status"] == "pending":
                done = engine.approve(c["id"], "scripted-demo-reviewer", c["digest"], "demo run")
                r = done.get("result") or {}
                pid = r.get("id") or r.get("batch_header", {}).get("payout_batch_id")
                print(f"   approve {c['action']:8} {c['amount']:>8} -> {done['status']}"
                      + (f" ({done['error']})" if done.get("error") else f", PayPal id {pid}"))
            else:
                engine.approve(c["id"], "scripted-demo-reviewer", c["digest"])  # expected to be refused
        except DecisionError as err:
            print(f"   approve {c['action']:8} {c['amount']:>8} -> refused: {err}")
    buys = [c for c in engine.cards if c["action"] == "purchase" and c["status"] == "executed"]
    if buys:
        print("\n3. Agentic commerce: the approved purchase is a PayPal checkout order; the owner pays it.")
        for c in buys:
            try:
                paid = engine.complete_purchase(c["id"], "scripted-demo-reviewer")
                how = " (payer approval simulated in mock mode)" if engine.paypal.mode == "mock" else ""
                print(f"   order {paid['result']['id']} -> {paid['result']['status']}, capture "
                      f"{paid['result'].get('capture_id')}{how}")
            except DecisionError as err:
                print(f"   order {c['result'].get('id')} -> not captured yet: {err}")
                if c["result"].get("approve_url"):
                    print(f"   pay it here as the sandbox buyer, then capture in the web UI: {c['result']['approve_url']}")
    ok, msg = engine.audit.verify()
    print(f"\n4. Audit log: {len(engine.audit.entries())} hash-chained entries, {msg}. "
          "Run `python3 -m countersign serve --fresh` to do this yourself in the browser.")
    return 0


def _save_capture(settings, ref: str, capture_id: str) -> None:
    path = settings.data_dir / "sandbox_orders.json"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    m = json.loads(path.read_text()) if path.exists() else {}
    m[ref] = capture_id
    path.write_text(json.dumps(m, indent=1))


def cmd_sandbox_seed(settings, card: bool = False) -> int:
    if settings.paypal_mode != "sandbox":
        print("Needs PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET (sandbox app) in .env")
        return 2
    pp = SandboxPayPal(settings.paypal_client_id, settings.paypal_client_secret)
    if not card:
        print("Open each approve link, log in with your *sandbox personal* account, approve; then run:\n"
              "  python -m countersign sandbox-capture <ORDER_REF> <ORDER_ID>\n"
              "(Or skip the logins: python -m countersign sandbox-seed --card)\n")
    for c in DEMO_CAPTURES.values():
        ref = c["invoice_id"]
        order = pp._call("POST", "/v2/checkout/orders", seed_order_body(ref, c["amount"], c["currency_code"], card),
                         str(uuid.uuid4()))
        if card:
            cap = order["purchase_units"][0]["payments"]["captures"][0]
            _save_capture(settings, ref, cap["id"])
            print(f"{ref}: order {order['id']} paid with the sandbox test card -> capture {cap['id']} "
                  f"({cap['amount']['value']} {cap['amount']['currency_code']}, {cap['status']}) saved")
        else:
            link = next((l["href"] for l in order.get("links", []) if l.get("rel") in ("approve", "payer-action")), "?")
            print(f"{ref}: order {order['id']}  approve: {link}")
    return 0


def cmd_sandbox_capture(settings, ref: str, order_id: str) -> int:
    pp = SandboxPayPal(settings.paypal_client_id, settings.paypal_client_secret)
    res = pp.capture_order(order_id)
    cap = res["purchase_units"][0]["payments"]["captures"][0]
    _save_capture(settings, ref, cap["id"])
    print(f"{ref} -> capture {cap['id']} ({cap['amount']['value']} {cap['amount']['currency_code']}) saved")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="countersign")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the approval web UI")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--fresh", action="store_true", help="reset the demo and run the agent before serving")
    s.add_argument("--replay", action="store_true", help="replay the recorded Claude run (docs/claude_run.json)")
    d = sub.add_parser("demo", help="headless end-to-end demo")
    d.add_argument("--replay", action="store_true", help="replay the recorded Claude run (docs/claude_run.json)")
    sub.add_parser("verify-audit", help="check the audit hash chain")
    sd = sub.add_parser("sandbox-seed", help="create sandbox orders for the demo inbox")
    sd.add_argument("--card", action="store_true",
                    help="pay them at once with PayPal's sandbox test card (no buyer login needed)")
    c = sub.add_parser("sandbox-capture", help="capture an approved sandbox order")
    c.add_argument("ref")
    c.add_argument("order_id")
    a = ap.parse_args(argv)
    if getattr(a, "replay", False):
        os.environ["COUNTERSIGN_PLANNER"] = "replay"
    try:
        settings = load_settings()
    except ConfigError as err:
        print(f"config error: {err}")
        return 2
    if a.cmd == "serve":
        from .web import serve
        engine = build(settings)
        if a.fresh:
            engine.reset()
            engine.run_agent()
        serve(engine, a.port)
        return 0
    if a.cmd == "demo":
        return cmd_demo(build(settings))
    if a.cmd == "verify-audit":
        ok, msg = build(settings).audit.verify()
        print(msg)
        return 0 if ok else 1
    if a.cmd == "sandbox-seed":
        return cmd_sandbox_seed(settings, a.card)
    if a.cmd == "sandbox-capture":
        return cmd_sandbox_capture(settings, a.ref, a.order_id)
    return 1


if __name__ == "__main__":
    sys.exit(main())
