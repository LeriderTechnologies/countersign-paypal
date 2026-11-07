"""Approval UI: a small server-rendered web page (stdlib http.server, no JS framework).

Binds to 127.0.0.1 only. Every POST carries a per-process CSRF token.
"""
from __future__ import annotations

import html
import json
import secrets
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import DecisionError, Engine

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#16202a;--mute:#5b6773;--line:#dde2e8;--ok:#127a3e;--bad:#b42318;
--warn:#a15c00;--acc:#0b57d0;--okbg:#e8f5ec;--badbg:#fdecea;--chip:#eef1f5}
@media (prefers-color-scheme:dark){:root{--bg:#0f1418;--card:#171e24;--ink:#e6edf3;--mute:#9aa7b3;--line:#2a343d;
--ok:#4ac27a;--bad:#ff7b72;--warn:#e3a23b;--acc:#7aa7ff;--okbg:#14301f;--badbg:#3a1714;--chip:#222b33}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,sans-serif}
header{padding:16px 20px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:20px;margin:0}h2{font-size:16px;margin:24px 0 8px}main{max-width:1100px;margin:0 auto;padding:0 16px 40px}
.chip{background:var(--chip);border-radius:999px;padding:2px 10px;font-size:13px;color:var(--mute)}
.chip.mock{color:var(--warn)}.chip.sandbox{color:var(--acc)}
.grid{display:grid;grid-template-columns:1fr 2fr;gap:16px}@media(max-width:800px){.grid{grid-template-columns:1fr}}
.box{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:12px}
.msg small{color:var(--mute)}.msg p{margin:4px 0 0;font-size:14px}
.card h3{margin:0 0 4px;font-size:16px}.amt{font-size:22px;font-weight:650}
.status{font-weight:600;text-transform:uppercase;font-size:12px;letter-spacing:.04em;padding:2px 8px;border-radius:6px}
.s-pending{background:var(--chip);color:var(--acc)}.s-blocked,.s-failed{background:var(--badbg);color:var(--bad)}
.s-executed{background:var(--okbg);color:var(--ok)}.s-rejected,.s-expired{background:var(--chip);color:var(--mute)}
ul.checks{list-style:none;padding:0;margin:6px 0;font-size:13px}ul.checks li{padding:1px 0}
.y{color:var(--ok)}.n{color:var(--bad);font-weight:600}
.why{color:var(--mute);font-size:14px;margin:6px 0}
form.inline{display:inline}button{font:inherit;border-radius:8px;border:1px solid var(--line);padding:6px 14px;
cursor:pointer;background:var(--card);color:var(--ink)}button.approve{background:var(--ok);color:#fff;border-color:var(--ok)}
button.reject{color:var(--bad)}button.primary{background:var(--acc);color:#fff;border-color:var(--acc)}
button:disabled{opacity:.45;cursor:not-allowed}input[type=text]{font:inherit;padding:5px 8px;border-radius:8px;
border:1px solid var(--line);background:var(--card);color:var(--ink);width:150px}
.flash{padding:10px 14px;border-radius:8px;margin:12px 0;background:var(--chip)}
table{width:100%;border-collapse:collapse;font-size:12.5px}td{border-top:1px solid var(--line);padding:4px 6px;vertical-align:top}
code{font-size:12px;word-break:break-all}.mute{color:var(--mute)}
.summary{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 12px}.stat{background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:8px 14px;min-width:150px}.stat b{display:block;font-size:22px}.stat span{color:var(--mute);font-size:13px}
.stat.bad b{color:var(--bad)}.stat.ok b{color:var(--ok)}.stat.acc b{color:var(--acc)}
.reason{background:var(--badbg);color:var(--bad);border-radius:8px;padding:6px 10px;margin:6px 0;font-size:14px}
.paybox{background:var(--chip);border-radius:8px;padding:8px 10px;margin:6px 0;font-size:14px}
details summary{cursor:pointer;color:var(--mute);font-size:13px}
.tag{font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--acc);margin-left:6px}
"""

ACTION_LABEL = {"refund": "Refund", "payout": "Payout", "invoice": "Invoice", "purchase": "Purchase"}

# Plain-English reason per failed check, shown at the top of a blocked card.
REASON = {
    "R1": "the action type is not allowed", "R2": "the amount is not valid", "R3": "the currency is not allowed",
    "R4": "it is over the per-action limit", "R5": "it would exceed today's outflow cap",
    "R6": "the payee is not on the human-vetted list", "R7": "it is more than PayPal says is refundable",
    "R8": "the invoice recipient is not an email address", "R9": "the same action was already proposed",
    "R10": "it does not match the vetted supplier catalogue",
    "V1": "the agent's evidence quote is not in the message", "V2": "the PayPal capture is not for that order",
    "V3": "the agent's claimed paid amount does not match PayPal", "V4": "the capture id is inconsistent",
    "V5": "the counterparty is not named in the message", "V6": "an item bought is not named in the message",
}
STATUS_ORDER = {"pending": 0, "blocked": 1, "executed": 2, "failed": 3, "rejected": 4, "expired": 5}


def e(x) -> str:
    return html.escape("" if x is None else str(x))


def _who(card: dict) -> str:
    p = card["params"]
    if card["action"] == "refund":
        return f"capture {p.get('capture_id')} (order {card['claims'].get('order_ref')})"
    if card["action"] == "payout":
        return f"to {p.get('receiver')}"
    if card["action"] == "invoice":
        lines = ", ".join(f"{i.get('quantity')} x {i.get('name')} @ {i.get('unit_price')}" for i in p.get("items", []))
        return f"to {p.get('recipient')}: {lines}"
    if card["action"] == "purchase":
        lines = ", ".join(f"{i.get('quantity')} x {i.get('sku')} {i.get('name')} @ {i.get('unit_price')}"
                          for i in p.get("items", []))
        return f"from {p.get('supplier')} via PayPal checkout: {lines}"
    return ""


def _reason(failed: list[dict]) -> str:
    parts = []
    for x in failed:
        r = REASON.get(x["rule"].split(" ", 1)[0])
        if r and r not in parts:
            parts.append(r)
    return "Blocked by code because " + "; ".join(parts) + "." if parts else "Blocked by code."


def _summary(state: dict) -> str:
    cards = state["cards"]
    if not cards:
        return ""
    n = lambda st: sum(c["status"] == st for c in cards)  # noqa: E731
    stopped = sum(float(c["amount"]) for c in cards if c["status"] == "blocked")
    moved = sum(float(c["amount"]) for c in cards if c["status"] == "executed")
    return (f'<div class="summary"><div class="stat acc"><b>{n("pending")}</b><span>waiting for your signature</span></div>'
            f'<div class="stat bad"><b>{n("blocked")}</b><span>blocked by code ({stopped:,.2f} stopped)</span></div>'
            f'<div class="stat ok"><b>{n("executed")}</b><span>approved, sent to PayPal ({moved:,.2f})</span></div></div>')


def render(state: dict, csrf: str, flash: str = "") -> str:
    cards_html = []
    order = {m["id"]: i for i, m in enumerate(state["inbox"])}
    for c in sorted(state["cards"], key=lambda c: (STATUS_ORDER.get(c["status"], 9), order.get(c["source_id"], 99))):
        allc = c["verification"] + c["rule_checks"]
        li = lambda x: (f'<li><span class="{"y" if x["ok"] else "n"}">{"PASS" if x["ok"] else "FAIL"}</span> '  # noqa: E731
                        f'{e(x["rule"])} <span class="mute">{e(x["detail"])}</span></li>')
        failed = [x for x in allc if not x["ok"]]
        passed = [x for x in allc if x["ok"]]
        checks = (f'<ul class="checks">{"".join(li(x) for x in failed)}</ul>' if failed else "")
        checks += (f'<details><summary>{len(passed)} of {len(allc)} checks passed (rules in code + claims vs PayPal)'
                   f'</summary><ul class="checks">{"".join(li(x) for x in passed)}</ul></details>')
        if c["status"] == "blocked":
            checks = f'<div class="reason">{e(_reason(failed))}</div>' + checks
        actions = ""
        if c["status"] == "pending":
            actions = (f'<form class="inline" method="post" action="/approve">'
                       f'<input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="card" value="{e(c["id"])}">'
                       f'<input type="hidden" name="digest" value="{e(c["digest"])}">'
                       f'<input type="text" name="who" placeholder="your name" required> '
                       f'<button class="approve">{"Approve &amp; create PayPal order" if c["action"] == "purchase" else "Approve &amp; execute"}</button></form> ')
        if c["status"] in ("pending", "blocked"):
            actions += (f'<form class="inline" method="post" action="/reject">'
                        f'<input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="card" value="{e(c["id"])}">'
                        f'<input type="hidden" name="who" value="human"><button class="reject">Reject</button></form>')
        if c["status"] == "blocked":
            actions = '<button disabled title="Blocked by code-enforced rules">Approve (blocked)</button> ' + actions
        result = ""
        if c.get("result") and c["action"] == "purchase":
            r = c["result"]
            if r.get("status") == "COMPLETED":
                result = (f'<div class="paybox"><b class="y">Paid.</b> PayPal order {e(r.get("id"))} captured '
                          f'({e(r.get("capture_id"))}).</div>')
            else:
                link = r.get("approve_url") or ""
                go = (f'<a href="{e(link)}" target="_blank" rel="noopener">open PayPal checkout</a>'
                      if link.startswith("https://") else "in mock mode there is no PayPal page; the button simulates it")
                label = "Simulate paying at PayPal &amp; capture" if state["paypal_mode"] == "mock" else "I paid at PayPal: capture"
                result = (f'<div class="paybox">PayPal order {e(r.get("id"))} created ({e(r.get("status"))}). '
                          f'Second signature: pay it as the shop owner ({go}).'
                          f'<form class="inline" method="post" action="/pay" style="margin-left:6px">'
                          f'<input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="card" value="{e(c["id"])}">'
                          f'<input type="text" name="who" placeholder="your name" required> '
                          f'<button class="approve">{label}</button></form></div>')
        elif c.get("result"):
            result = f'<div class="mute">PayPal ({e(state["paypal_mode"])}) response: <code>{e(json.dumps(c["result"])[:300])}</code></div>'
        if c.get("error"):
            result = f'<div class="n">PayPal error: {e(c["error"])}</div>'
        decided = (f'<div class="mute">Decided by {e(c["decided_by"])} at {e(c["decided_at"])}</div>'
                   if c.get("decided_by") else "")
        cards_html.append(f"""
<div class="box card"><div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap">
<h3>{e(ACTION_LABEL.get(c["action"], c["action"]))}{'<span class="tag">agentic commerce</span>' if c["action"] == "purchase" else ""} <span class="mute">· from {e(c["source_id"])} “{e(c["source_subject"])}”</span></h3>
<span class="status s-{e(c["status"])}">{e(c["status"])}</span></div>
<div class="amt">{e(c["amount"])} {e(c["currency"])}</div><div>{e(_who(c))}</div>
<div class="why"><b>Agent's reasoning:</b> {e(c["rationale"])}</div>
{checks}{actions}{result}{decided}
<div class="mute" style="font-size:12px">card {e(c["id"])} · digest {e(c["digest"][:16])}… · expires {e(c["expires_at"])} · {e(c["planner"])}</div></div>""")

    inbox_html = "".join(
        f'<div class="box msg"><small>{e(m["id"])} · {e(m["from"])}</small><div><b>{e(m["subject"])}</b></div>'
        f'<p>{e(m["body"])}</p></div>' for m in state["inbox"])
    notes_html = "".join(f'<div class="box mute">No action on {e(n.get("source_id"))}: {e(n.get("reason"))}</div>'
                         for n in state["notes"])
    audit_rows = "".join(
        f'<tr><td>{e(a["ts"][11:19])}</td><td>{e(a["actor"])}</td><td>{e(a["event"])}</td>'
        f'<td><code>{e(json.dumps(a["details"])[:160])}</code></td><td><code>{e(a["hash"][:10])}</code></td></tr>'
        for a in reversed(state["audit"]))
    pol = state["policy"]
    payees = ", ".join(pol["payees"])
    limits = ", ".join(f"{k} ≤ {v}" for k, v in pol["max_per_action"].items())
    flash_html = f'<div class="flash">{e(flash)}</div>' if flash else ""
    chain = (f'<span class="y">audit chain intact</span>' if state["audit_ok"]
             else f'<span class="n">AUDIT CHAIN BROKEN: {e(state["audit_msg"])}</span>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Countersign</title><style>{CSS}</style></head>
<body><header><h1>Countersign</h1><span class="mute">The AI proposes. A human countersigns. Code holds the line.</span>
<span class="chip {e(state["paypal_mode"])}">PayPal: {e(state["paypal_mode"]).upper()}</span>
<span class="chip">Agent: {e(state["planner"])}</span></header><main>{flash_html}
<div class="box" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:16px">
<form class="inline" method="post" action="/run"><input type="hidden" name="csrf" value="{csrf}">
<button class="primary">Run agent on inbox</button></form>
<form class="inline" method="post" action="/reset"><input type="hidden" name="csrf" value="{csrf}">
<button>Reset demo</button></form>
<span class="mute">Rules (Python, not prompt): {e(limits)} · daily outflow cap {e(pol["daily_outflow_cap"])} (today {e(state["outflow_today"])})
· vetted payees: {e(payees)} · vetted supplier SKUs: {e(", ".join(pol.get("catalog", {})))} · {chain}</span></div>
<div class="grid"><section><h2>Inbox</h2>{inbox_html}</section>
<section><h2>Decision cards</h2>{_summary(state)}{notes_html}{"".join(cards_html) or '<div class="box mute">No cards yet. Run the agent.</div>'}</section></div>
<h2>Audit log (hash-chained, newest first)</h2><div class="box"><table>{audit_rows}</table></div>
</main></body></html>"""


def make_handler(engine: Engine, csrf: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8", extra=None):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, msg: str):
            self.send_response(303)
            self.send_header("Location", "/?msg=" + urllib.parse.quote(msg))
            self.end_headers()

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/api/state":
                return self._send(200, json.dumps(engine.state(), default=str), "application/json")
            if url.path != "/":
                return self._send(404, "not found", "text/plain")
            msg = urllib.parse.parse_qs(url.query).get("msg", [""])[0]
            return self._send(200, render(engine.state(), csrf, msg))

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(min(n, 65536)).decode()).items()}
            if not secrets.compare_digest(form.get("csrf", ""), csrf):
                return self._send(403, "bad csrf token", "text/plain")
            path = urllib.parse.urlsplit(self.path).path
            try:
                if path == "/run":
                    cards = engine.run_agent()
                    blocked = sum(c["status"] == "blocked" for c in cards)
                    return self._redirect(f"Agent proposed {len(cards)} action(s); {blocked} blocked by rules or verification.")
                if path == "/approve":
                    c = engine.approve(form.get("card", ""), form.get("who", "").strip(), form.get("digest", ""))
                    return self._redirect(f"{c['id']}: {c['status']}" + (f" ({c['error']})" if c.get("error") else ""))
                if path == "/pay":
                    c = engine.complete_purchase(form.get("card", ""), form.get("who", "").strip())
                    return self._redirect(f"{c['id']}: purchase paid, PayPal order {c['result'].get('status')}")
                if path == "/reject":
                    c = engine.reject(form.get("card", ""), form.get("who", "human"))
                    return self._redirect(f"{c['id']}: rejected")
                if path == "/reset":
                    engine.reset()
                    return self._redirect("Demo reset.")
            except DecisionError as err:
                return self._redirect(f"Refused: {err}")
            return self._send(404, "not found", "text/plain")

    return Handler


def serve(engine: Engine, port: int = 8765) -> None:
    csrf = secrets.token_urlsafe(24)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(engine, csrf))
    print(f"Countersign running at http://127.0.0.1:{port}  (PayPal: {engine.paypal.mode}, agent: {engine.planner.name})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
