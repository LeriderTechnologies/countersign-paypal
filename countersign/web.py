"""Approval UI: a small server-rendered web page (stdlib http.server, no JS framework).

Binds to 127.0.0.1 only. Every page except the login form needs a signed-in approver (see auth.py);
every POST carries the session's CSRF token. The approver identity comes from the session, never from a form.
"""
from __future__ import annotations

import html
import json
import re
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .auth import AUTH_METHOD, COOKIE, SESSION_TTL, Auth, AuthError
from .engine import DecisionError, Engine

STATIC = Path(__file__).resolve().parent / "static"
# Files the server may send from countersign/static (fixed list: no path from the URL ever reaches the filesystem).
STATIC_FILES = {"Geist.woff2": "font/woff2", "GeistMono.woff2": "font/woff2", "mark.svg": "image/svg+xml",
                "mark-white.svg": "image/svg+xml"}

# Lerider brand v2 tokens. Geist / Geist Mono are self-hosted (countersign/static, SIL OFL 1.1): no CDN at runtime.
CSS = """
@font-face{font-family:Geist;src:url(/static/Geist.woff2) format("woff2");font-weight:100 900;font-display:swap}
@font-face{font-family:"Geist Mono";src:url(/static/GeistMono.woff2) format("woff2");font-weight:100 900;font-display:swap}
:root{--bone:#F3F1EA;--ink:#16181D;--slate:#5E6168;--stone:#DDD9CE;--paper:#FFFFFF;--sand:#EDEAE1;--wash:#F8F7F2;
--foot:#FBFAF6;--signal:#E2462F;--mono:"Geist Mono",ui-monospace,SFMono-Regular,Menlo,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bone);color:var(--ink);font:15px/1.5 Geist,system-ui,-apple-system,"Segoe UI",sans-serif;
font-feature-settings:"tnum";-webkit-font-smoothing:antialiased}
a{color:var(--ink)}a:hover{color:var(--slate)}
h1,h2,h3,p{margin:0}
code,.mono{font-family:var(--mono);font-size:12.5px}
.mute{color:var(--slate)}
button{font:inherit;font-weight:500;min-height:44px;padding:0 18px;border-radius:10px;border:1px solid var(--stone);
background:var(--paper);color:var(--ink);cursor:pointer}
button:hover{border-color:var(--ink)}
button.primary,button.approve{background:var(--ink);color:var(--bone);border-color:var(--ink);font-weight:600;padding:0 22px}
button.primary:hover,button.approve:hover{background:#2A2D33}
button:disabled{border:1px dashed #B9B4A7;background:transparent;color:var(--slate);font-weight:400;cursor:not-allowed}
button.small{min-height:32px;padding:0 12px;font-size:13px}
form.inline{display:inline}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--signal);flex:none}
.dot.ink{background:var(--ink)}
/* header */
header{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:16px;padding:16px max(40px,calc(50% - 640px));
border-bottom:1px solid var(--stone);background:var(--bone)}
.brand{display:flex;align-items:center;gap:12px}.brand img{width:28px;height:28px;display:block}
.brand .name{font-weight:600;font-size:18px;letter-spacing:-.01em}
.brand .ctx{color:var(--slate);font-size:13px;padding-left:12px;border-left:1px solid var(--stone);display:flex;gap:10px}
.chip{color:var(--slate)}
nav{display:flex;align-items:center;gap:28px;font-size:14px}
nav a{text-decoration:none;color:var(--slate);padding:4px 0;border-bottom:2px solid transparent}
nav a.on{color:var(--ink);font-weight:600;border-bottom-color:var(--ink)}
.who{display:flex;align-items:center;gap:10px;padding:4px 6px 4px 6px;border:1px solid var(--stone);border-radius:999px;
background:var(--paper);font-size:13px}
.av{width:24px;height:24px;border-radius:50%;background:var(--ink);color:var(--bone);font-size:10.5px;font-weight:600;
display:inline-flex;align-items:center;justify-content:center;flex:none;letter-spacing:.02em}
.who b{font-weight:600}
/* layout */
main{max-width:1360px;margin:0 auto;padding:28px 40px 64px;display:flex;flex-wrap:wrap;gap:32px;align-items:flex-start}
aside{flex:1 1 260px;max-width:300px;display:flex;flex-direction:column;gap:16px}
.col{flex:999 1 640px;min-width:0;display:flex;flex-direction:column;gap:18px}
.panel{background:var(--paper);border:1px solid var(--stone);border-radius:14px;padding:20px}
.panel.plain{background:transparent}
.eyebrow{font-size:12px;font-weight:500;color:var(--slate);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px}
.big{font-size:28px;font-weight:600;letter-spacing:-.02em}.big span{font-size:15px;font-weight:400;color:var(--slate);letter-spacing:0}
.meter{height:6px;background:var(--sand);border-radius:3px;margin-top:12px}.meter i{display:block;height:6px;background:var(--ink);border-radius:3px}
.kv{display:flex;justify-content:space-between;gap:12px;padding:5px 0}.kv b{font-weight:500;text-align:right}
.small{font-size:13px}.note{font-size:13px;color:var(--slate);margin-top:10px}
.chain{display:flex;align-items:center;gap:8px}
.flash{background:var(--paper);border:1px solid var(--ink);border-radius:10px;padding:12px 16px}
.titlerow{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px}
.titlerow h1{font-size:30px;font-weight:600;letter-spacing:-.02em}
.actions{display:flex;gap:10px;flex-wrap:wrap}
.summary{display:inline-flex;gap:4px;padding:4px;background:var(--sand);border-radius:10px;font-size:14px}
.summary span{padding:6px 14px;border-radius:7px;color:var(--slate)}.summary span.on{background:var(--paper);color:var(--ink);font-weight:500}
.intro{color:var(--slate)}
/* decision cards */
.card{background:var(--paper);border:1px solid var(--stone);border-radius:14px;overflow:hidden}
.card .body{padding:22px 28px;display:flex;flex-direction:column;gap:16px}
.top{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}.top>div{flex:1 1 auto;min-width:0}
.card h3{font-size:13px;font-weight:400;color:var(--slate)}
.tag{font-weight:500;color:var(--ink)}
.amt{font-size:32px;font-weight:600;letter-spacing:-.02em;margin-top:4px}
.cp{margin-top:2px}
.st{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border:1px solid var(--ink);border-radius:999px;
font-size:13px;font-weight:500;white-space:nowrap}
.st.blocked{background:var(--ink);color:var(--bone)}.st.quiet{border-color:var(--stone);color:var(--slate)}
.why{background:var(--wash);border-radius:10px;padding:14px 16px}
.why p:first-child,.reason p:first-child{font-size:13px;font-weight:500;color:var(--slate)}
.why p+p{margin-top:4px}
.reason{border:1px solid var(--ink);border-radius:10px;padding:14px 16px}
.reason p:first-child{color:var(--ink);font-weight:600;font-size:15px}.reason p+p{margin-top:4px}
ul.checks{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px 20px;font-size:14px}
ul.checks li{display:flex;gap:8px;align-items:flex-start}ul.checks svg{flex:none;margin-top:3px}
ul.checks li.n{font-weight:500}ul.checks li small{display:block;font-weight:400;color:var(--slate);font-size:12.5px}
ul.checks li.count{color:var(--slate)}
details.all summary{cursor:pointer;color:var(--slate);font-size:13px;font-weight:500;list-style:none;display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border:1px solid var(--stone);border-radius:999px;background:var(--paper)}
details.all summary::-webkit-details-marker{display:none}details.all summary::marker{content:""}
details.all summary:hover{border-color:var(--ink);color:var(--ink)}
details.all summary:focus-visible{outline:2px solid var(--ink);outline-offset:2px}
details.all summary svg{transition:transform .15s}details.all[open] summary svg{transform:rotate(180deg)}
details.all .hide{display:none}details.all[open] .hide{display:inline}details.all[open] .show{display:none}
details.all ul{list-style:none;margin:10px 0 0;padding:0;font-size:13px}details.all li{padding:6px 0;border-top:1px solid var(--sand);display:flex;gap:8px;align-items:flex-start}details.all li svg{flex:none;margin-top:3px}details.all li b{font-weight:500}
details.all code{color:var(--slate)}
.meta{font-size:13px;color:var(--slate)}
.foot{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;padding:14px 28px;
border-top:1px solid var(--sand);background:var(--foot)}
.foot .sig{font-size:13px;color:var(--slate)}.foot .sig b{color:var(--ink);font-weight:600}
.foot .btns{display:flex;gap:10px;flex-wrap:wrap}
.paybox{border:1px solid var(--stone);border-radius:10px;padding:14px 16px;background:var(--wash);display:flex;
flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between}
.paybox p+p{margin-top:2px}
.err{border:1px solid var(--ink);border-radius:10px;padding:12px 16px}
.decided{font-size:13px;color:var(--slate)}.decided.invalid{color:var(--ink);border:1px solid var(--ink);border-radius:10px;padding:8px 12px;text-align:left}.decided b{color:var(--ink);font-weight:600}
.card.done{padding:18px 28px;display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px 16px;align-items:center}
.card.done .amt{font-size:20px;margin-top:2px}.card.done .amt span{font-size:14px;font-weight:400;color:var(--slate);letter-spacing:0}
.card.done .right{text-align:right;display:flex;flex-direction:column;gap:2px;margin-left:auto}
.empty{border:1px dashed var(--stone);border-radius:14px;padding:22px 28px;color:var(--slate)}
.skip{font-size:13px;color:var(--slate);padding:0 4px}
/* inbox */
.inbox{background:var(--paper);border:1px solid var(--stone);border-radius:14px}
.msg{padding:14px 20px;border-top:1px solid var(--sand)}.msg:first-child{border-top:0}
.msg .from{font-size:13px;color:var(--slate)}.msg b{font-weight:600}.msg p{margin-top:2px;font-size:14px}
h2.sec{font-size:20px;font-weight:600;letter-spacing:-.01em}
/* audit */
.audit-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-end;gap:16px;width:100%}
.audit-head p{max-width:640px;margin-top:4px}
.badge{display:flex;align-items:center;gap:12px;padding:12px 16px;background:var(--paper);border:1px solid var(--stone);border-radius:12px}
.badge b{font-size:14px;display:block}.badge.bad{border-color:var(--ink)}
.tablewrap{overflow-x:auto;background:var(--paper);border:1px solid var(--stone);border-radius:14px;width:100%}
table{width:100%;min-width:900px;border-collapse:collapse;font-size:14px}
th{white-space:nowrap;text-align:left;color:var(--slate);font-size:12px;text-transform:uppercase;letter-spacing:.06em;font-weight:500;
padding:14px 20px;border-bottom:1px solid var(--stone)}
td{padding:12px 20px;border-bottom:1px solid var(--sand);vertical-align:top}tr:last-child td{border-bottom:0}
td.t,td.h{font-family:var(--mono);font-size:13px}td.h{color:var(--slate)}
.ev{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;background:var(--sand);font-size:13px;white-space:nowrap}
.ev.signed{background:var(--ink);color:var(--bone)}.ev.blocked{background:transparent;border:1px solid var(--ink)}
.person{display:inline-flex;align-items:center;gap:8px}.person .av{width:22px;height:22px;font-size:10px}
/* sign-in */
.login{display:flex;flex-wrap:wrap;min-height:100vh}
.login .side{flex:1 1 480px;background:var(--ink);color:var(--bone);padding:48px 56px;display:flex;flex-direction:column;
justify-content:space-between;gap:40px}
.login .side h1{font-size:44px;line-height:1.1;font-weight:600;letter-spacing:-.02em}
.login .side p.lead{margin-top:20px;font-size:17px;color:#C9C6BD;max-width:460px}
.login .side .by{font-size:13px;color:#A9A69D}
.login .side .brand img{width:32px;height:32px}.login .side .brand .name{font-size:20px}
.login .formside{flex:1 1 480px;display:flex;align-items:center;justify-content:center;padding:48px 24px}
.login form{width:100%;max-width:400px;display:flex;flex-direction:column;gap:18px}
.login h2{font-size:26px;font-weight:600;letter-spacing:-.01em}.login h2+p{margin-top:6px;color:var(--slate)}
.field{display:flex;flex-direction:column;gap:6px}.field label{font-size:14px;font-weight:500}
input[type=text],input[type=password]{font:inherit;min-height:46px;padding:0 14px;border:1px solid #8A8578;
border-radius:10px;background:var(--paper);color:var(--ink);width:100%}
input:focus{outline:2px solid var(--ink);outline-offset:0;border-color:var(--ink)}
.login button.primary{min-height:48px;font-size:16px}
.lock{display:flex;gap:10px;align-items:flex-start;padding:14px 16px;border:1px solid var(--stone);border-radius:10px;
font-size:13px;color:var(--slate);background:var(--foot)}.lock svg{flex:none;margin-top:1px}
@media(max-width:800px){header,main{padding-left:16px;padding-right:16px}ul.checks{grid-template-columns:1fr}
aside{max-width:none}.login .side{padding:32px 24px}}
"""

ICON_OK = ('<svg aria-hidden="true" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="#16181D" '
           'stroke-width="1.8"><path d="M3 8.5l3 3 7-7"/></svg>')
ICON_NO = ('<svg aria-hidden="true" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="#16181D" '
           'stroke-width="1.8"><path d="M4 4l8 8M12 4l-8 8"/></svg>')
ICON_LOCK = ('<svg aria-hidden="true" width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="#16181D" '
             'stroke-width="1.6"><rect x="3.5" y="8" width="11" height="7.5" rx="1.5"/><path d="M6 8V5.8a3 3 0 016 0V8"/></svg>')
ICON_SHIELD = ('<svg aria-hidden="true" width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="#16181D" '
               'stroke-width="1.8"><path d="M10 2.5l6 2.5v4.5c0 4-2.6 6.6-6 8-3.4-1.4-6-4-6-8V5z"/><path d="M 7.2 10l2 2 3.6-3.8"/></svg>')

ICON_CHEVRON = ('<svg aria-hidden="true" width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" '
                'stroke-width="1.6"><path d="M 2.5 4.5l3.5 3.5 3.5-3.5"/></svg>')

ACTION_LABEL = {"refund": "Refund", "payout": "Payout", "invoice": "Invoice", "purchase": "Purchase"}

# Plain-English reason per failed check, shown at the top of a blocked card.
REASON = {
    "R1": "the action type is not allowed", "R2": "the amount is not valid", "R3": "the currency is not allowed",
    "R4": "it is over the {limit}per-action limit", "R5": "it would break today's {cap}outflow cap",
    "R6": "the payee is not on your vetted list", "R7": "it is more than PayPal says is refundable",
    "R8": "the invoice recipient is not an email address", "R9": "the same action was already proposed",
    "R10": "it does not match the vetted supplier catalogue",
    "V1": "the agent's evidence quote is not in the message", "V2": "the PayPal capture is not for that order",
    "V3": "the agent's claimed paid amount does not match PayPal", "V4": "the capture id is inconsistent",
    "V5": "the counterparty is not named in the message", "V6": "an item bought is not named in the message",
}
# Short check labels as the approver reads them: (when passed, when failed).
CHECK_LABEL = {
    "R1": ("Allowed action", "Action not allowed"), "R2": ("Valid amount", "Invalid amount"),
    "R3": ("Allowed currency", "Currency not allowed"), "R4": ("Under per-action limit", "Over per-action limit"),
    "R5": ("Within daily cap", "Exceeds daily cap"), "R6": ("Payee is vetted", "Payee not vetted"),
    "R7": ("Within refundable amount", "More than refundable"), "R8": ("Recipient is an email", "Recipient not an email"),
    "R9": ("No duplicate", "Already proposed"), "R10": ("Matches supplier catalogue", "Off the supplier catalogue"),
    "V1": ("Quote found in message", "Quote not in message"),
    "V2": ("Capture matches the order", "Capture not for this order"),
    "V3": ("Paid amount matches PayPal", "Paid amount differs from PayPal"),
    "V4": ("Capture id consistent", "Capture id mismatch"),
    "V5": ("Counterparty named in message", "Counterparty not in message"),
    "V6": ("Items named in message", "Item not in message"),
}
# PayPal status codes in plain words (audit log and done rows).
PAYPAL_STATUS = {"PAYER_ACTION_REQUIRED": "waiting for the buyer to approve in PayPal", "COMPLETED": "completed",
                 "PENDING": "pending at PayPal", "SUCCESS": "paid out", "PROCESSING": "processing at PayPal",
                 "SENT": "sent", "CREATED": "created", "APPROVED": "approved by the buyer",
                 "PARTIALLY_REFUNDED": "partly refunded", "REFUNDED": "refunded"}
# The only texts a ?msg= key can show (no free text from the URL: no content spoofing). Messages that
# carry details (card ids, errors) are kept server-side in the session and shown once via msg=flash.
MESSAGES = {"signed_in": "Signed in.", "signed_out": "Signed out.", "reset": "Demo reset."}
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


def _reason(failed: list[dict], limit: str = "", cap: str = "") -> str:
    """The plain-English reason a card was blocked: 'It is over the $500 per-action limit, and ...'."""
    parts = []
    for x in failed:
        r = REASON.get(x["rule"].split(" ", 1)[0])
        if r:
            r = r.format(limit=f"{limit} " if limit else "", cap=f"{cap} " if cap else "")
            if r not in parts:
                parts.append(r)
    if not parts:
        return "A rule in code failed, so this cannot be approved."
    text = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + ", and " + parts[-1]
    return text[:1].upper() + text[1:] + "."


def _num(x: str, cur: str = "USD") -> str:
    """'4900.00' -> '$4,900' (cents kept when there are any)."""
    return money(x, cur, compact=True)


def check_label(rule: str, ok: bool) -> str:
    code, text = _label(rule)
    pair = CHECK_LABEL.get(code)
    return pair[0 if ok else 1] if pair else text


def plain_detail(rule: str, ok: bool, detail: str, cur: str = "USD") -> str:
    """Turn a check's machine detail ('4900.00 <= 500.00') into a sentence ('$4,900 is over the $500 limit')."""
    code = rule.split(" ", 1)[0]
    d = detail or ""
    m = None
    if code == "R1":
        act = d.partition("=")[2].strip("'\"") or "this action"
        return f"{act.capitalize()} is an allowed action" if ok else f"{act.capitalize()} is not an action Countersign allows"
    if code == "R2" and ok:
        m = re.match(r"(\S+) (\S+)", d)
        return f"Amount {_num(m.group(1), m.group(2))}" if m else d
    if code == "R3":
        c = d.split(" ", 1)[0]
        return f"{c} is allowed" if ok else f"{c} is not an allowed currency"
    if code == "R4" and (m := re.match(r"(\S+) <= (\S+)", d)):
        a, b = _num(m.group(1), cur), _num(m.group(2), cur)
        return f"{a} is within the {b} limit" if ok else f"{a} is over the {b} limit"
    if code == "R5" and (m := re.search(r"(\S+) <= (\S+)$", d)):
        a, b = _num(m.group(1), cur), _num(m.group(2), cur)
        return (f"Today's outflow with this: {a} of {b}" if ok else
                f"Today's outflow with this would be {a}, over the {b} cap")
    if code == "R6":
        if ok:
            return f"Vetted payee: {d}"
        return d.replace(" is NOT on the allowlist", " is not on the vetted payee list")
    if code == "R7" and (m := re.match(r"(\S+) (\S+) <= refundable (\S+) (\S+) \(capture status (\S+)\)", d)):
        a, b = _num(m.group(1), m.group(2)), _num(m.group(3), m.group(4))
        st = m.group(5)
        tail = "" if st in ("COMPLETED", "PARTIALLY_REFUNDED") else f"; the PayPal capture is {PAYPAL_STATUS.get(st, st.lower())}"
        return (f"{a} of {b} still refundable" if ok else f"{a} is more than the {b} still refundable") + tail
    if code == "R7":
        return d[:1].upper() + d[1:]
    if code == "R8":
        return f"Sent to {d}" if ok else f"{d} is not an email address"
    if code == "R9":
        return "First time this action is proposed" if ok else d[:1].upper() + d[1:].replace("in card ", "as ")
    if code == "R10":
        if ok:
            return "Every line matches the vetted supplier, price and quantity"
        d = re.sub(r"price (\S+) (\S+) != catalogue (\S+) (\S+)",
                   lambda k: f"costs {_num(k.group(1), k.group(2))}, the catalogue says {_num(k.group(3), k.group(4))}", d)
        d = re.sub(r"quantity (\S+) > max (\S+)", r"quantity \1 is over the maximum of \2", d)
        d = d.replace("'", "")
        return d[:1].upper() + d[1:]
    if code == "V1":
        if d == "no quote given":
            return "The agent gave no quote"
        return f"Found in the message: {d}" if ok else f"Not in the message: {d}"
    if code == "V2":
        if (m := re.match(r"PayPal capture \S+ is for order (.+), agent said (.+)$", d)):
            at, said = m.group(1).strip("'\""), m.group(2).strip("'\"")
            return f"PayPal capture is for order {at}" if ok else f"PayPal says order {at}, the agent said {said}"
        return d.replace("'", "")[:1].upper() + d.replace("'", "")[1:]
    if code == "V3" and (m := re.match(r"agent: (\S+), PayPal: (\S+) (\S+)", d)):
        a, b = _num(m.group(1), m.group(3)), _num(m.group(2), m.group(3))
        return f"Agent and PayPal agree: {b}" if ok else f"Agent says {a}, PayPal captured {b}"
    if code == "V4" and (m := re.match(r"(\S+) vs (\S+)", d)):
        return f"Card says {m.group(1)}, PayPal says {m.group(2)}"
    if code == "V5":
        return f"{d} is in the message" if ok else f"{d} is not in the message"
    if code == "V6":
        if (m := re.search(r"\(not in message: (.+)\)$", d)):
            return f"Not in the message: {m.group(1)}"
        return f"Named in the message: {d}"
    return d.replace("<=", "within").replace("!=", "differs from")


def who_label(user: str, method: str | None) -> str:
    """How a decision's identity is shown: '<user> (authenticated)' only for a password-session login."""
    if method == AUTH_METHOD:
        return f"{user} (authenticated)"
    return f"{user} (not authenticated: {method or 'name typed'})"


SYMBOL = {"USD": "$", "EUR": "€", "GBP": "£"}
HUMAN_ACTORS_NOT = {"agent", "executor", "system", "human"}
ACTOR_LABEL = {"executor": "Countersign", "system": "Countersign"}
DONE_VERB = {"refund": "refunded at PayPal", "payout": "paid out via PayPal", "invoice": "invoice sent via PayPal",
             "purchase": "paid and captured at PayPal"}
APPROVE_VERB = {"refund": "Approve and refund", "payout": "Approve and pay", "invoice": "Approve and send invoice",
                "purchase": "Approve and create PayPal order"}


def money(amount, currency, compact: bool = False) -> str:
    try:
        d = Decimal(str(amount))
        v = f"{d:,.0f}" if compact and d == d.to_integral_value() else f"{d:,.2f}"
    except (InvalidOperation, ValueError):
        return f"{amount} {currency}"
    sym = SYMBOL.get(str(currency))
    return f"{sym}{v}" if sym else f"{v} {currency}"


def initials(user: str) -> str:
    parts = [p for p in re.split(r"[._\-\s@]+", user or "") if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return (user or "?")[:2].upper()


def _label(rule: str) -> tuple[str, str]:
    """'R6 payee on human-vetted allowlist' -> ('R6', 'Payee on human-vetted allowlist')."""
    code, _, text = rule.partition(" ")
    if not text:
        return "", rule
    return code, text[:1].upper() + text[1:]


DEMO_AGENT = "Demo agent (scripted, no AI)"


def agent_label(planner: str) -> str:
    """Planner names are written for logs ('mock planner (offline, deterministic)'); show product wording."""
    if planner.startswith("mock planner"):
        return DEMO_AGENT
    m = re.match(r"Claude \(([^)]*)\), replay of a recorded run \(([^)]*)\)", planner)
    if m:
        return f"Agent: Claude, recorded run of {m.group(2)}"
    return f"Agent: {planner}" if planner else "Agent"


def _head(user: str | None, csrf: str, state: dict | None, active: str) -> str:
    mode = (state or {}).get("paypal_mode", "")
    ctx = ""
    if state is not None:
        mode_txt = "PayPal sandbox" if mode == "sandbox" else ("Demo mode · offline" if mode == "mock" else f"PayPal {mode}")
        agent = agent_label(state.get("planner") or "")
        agent_html = "" if (mode == "mock" and agent == DEMO_AGENT) else f'<span>{e(agent)}</span>'
        ctx = f'<span class="ctx"><span class="chip {e(mode)}">{e(mode_txt)}</span>{agent_html}</span>'
    on = ' class="on"'
    nav = "".join(f'<a href="{href}"{on if key == active else ""}>{label}</a>'
                  for key, href, label in (("decisions", "/", "Decisions"), ("audit", "/audit", "Audit log"),
                                           ("rules", "/#rules", "Rules")))
    account = (f'<span class="who"><span class="av">{e(initials(user))}</span><span>Signed in as <b>{e(user)}</b></span>'
               f'<form class="inline" method="post" action="/logout"><input type="hidden" name="csrf" value="{e(csrf)}">'
               f'<button class="small">Log out</button></form></span>' if user else "")
    return (f'<header><div class="brand"><img src="/static/mark.svg" alt=""><span class="name">Countersign</span>{ctx}</div>'
            f'<nav aria-label="Main">{nav}{account}</nav></header>')


def _page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title>'
            f'<link rel="icon" href="/static/mark.svg"><style>{CSS}</style></head><body>{body}</body></html>')


def render_login(token: str, flash: str = "", has_accounts: bool = True) -> str:
    flash_html = f'<div class="flash">{e(flash)}</div>' if flash else ""
    none = ("" if has_accounts else '<div class="flash">No approver accounts exist yet. Create one in a terminal: '
            '<code>python3 -m countersign add-approver &lt;name&gt;</code></div>')
    return _page("Countersign: sign in", f"""<div class="login">
<section class="side"><div class="brand"><img src="/static/mark-white.svg" alt=""><span class="name">Countersign</span></div>
<div><h1>The AI proposes.<br>You countersign.</h1>
<p class="lead">Your agent drafts PayPal refunds, payouts, invoices and supplier orders. Rules in code stop what should
never happen. Every approval carries the name of the person who signed in.</p></div>
<p class="by">A Lerider Technologies product</p></section>
<section class="formside"><form method="post" action="/login"><input type="hidden" name="login_token" value="{e(token)}">
<div><h2>Sign in to approve</h2><p>Only approvers set up by the shop owner can sign, so nobody can
approve in someone else's name. Your name is taken from this login, never typed next to a button.</p></div>{flash_html}{none}
<div class="field"><label for="u">Approver</label><input id="u" type="text" name="username" autocomplete="username" spellcheck="false" autocapitalize="off" autocorrect="off" required></div>
<div class="field"><label for="p">Password</label><input id="p" type="password" name="password" autocomplete="current-password" required></div>
<button class="primary">Sign in</button>
<div class="lock">{ICON_LOCK}<span>Passwords are stored as salted scrypt hashes. Your name goes into the approval record
and the audit log, both HMAC-signed with a server key.</span></div></form></section></div>""")


def _counterparty(c: dict, payees: dict, failed_codes: set) -> str:
    p = c["params"]
    if c["action"] == "payout":
        rcv = p.get("receiver") or ""
        name = payees.get(rcv) if isinstance(payees, dict) else None
        vetted = "not vetted" if "R6" in failed_codes else "vetted payee"
        return f"to {e(name)} · {e(rcv)} · {vetted}" if name else f"to {e(rcv)} · {vetted}"
    if c["action"] == "refund":
        return e(f"to the buyer of order {c['claims'].get('order_ref')} · PayPal capture {p.get('capture_id')}")
    return e(_who(c))


def _checks(c: dict) -> str:
    allc = c["rule_checks"] + c["verification"]
    failed = [x for x in allc if not x["ok"]]
    passed = [x for x in allc if x["ok"]]
    cur = c.get("currency") or "USD"

    def li(x, show_detail):
        plain = plain_detail(x["rule"], x["ok"], x["detail"], cur)
        det = f'<small>{e(plain)}</small>' if show_detail else ""
        return (f'<li class="{"y" if x["ok"] else "n"}" title="{e(plain)}">{ICON_OK if x["ok"] else ICON_NO}'
                f'<span>{e(check_label(x["rule"], x["ok"]))}{det}</span></li>')
    # Passed checks worth showing first (like the mockup); the trivial ones (R1-R3) stay in "All N checks".
    prio = ["R4", "R5", "R6", "R7", "R10", "V3", "V2", "V5", "V6", "V1", "R8", "R9"]
    rank = lambda x: prio.index(x["rule"].split(" ", 1)[0]) if x["rule"].split(" ", 1)[0] in prio else 99  # noqa: E731
    shown = [li(x, True) for x in failed] if failed else [li(x, False) for x in sorted(passed, key=rank)[:5]]
    shown.append(f'<li class="count">{len(passed)} of {len(allc)} checks passed</li>')
    full = "".join(f'<li>{ICON_OK if x["ok"] else ICON_NO}<span>{"<b>" if not x["ok"] else ""}'
                   f'{e(check_label(x["rule"], x["ok"]))}{"</b>" if not x["ok"] else ""}'
                   f' <span class="mute">· {e(plain_detail(x["rule"], x["ok"], x["detail"], cur))}</span></span></li>'
                   for x in allc)
    return (f'<ul class="checks">{"".join(shown)}</ul>'
            f'<details class="all"><summary><span class="show">All {len(allc)} checks</span>'
            f'<span class="hide">Hide checks</span>{ICON_CHEVRON}</summary>'
            f'<p class="note">Rules in code, plus the agent\'s claims checked against PayPal and the message.</p>'
            f'<ul>{full}</ul></details>')


def _decided(c: dict, invalid: bool = False) -> str:
    out = ""
    if invalid:
        out = (f'<div class="decided invalid"><span class="dot"></span> <b>Approval signature INVALID.</b> The record names '
               f'{e(c.get("decided_by"))}, but its HMAC does not match: it was edited outside Countersign. '
               f'Run verify-audit; the audit log is the source of truth.</div>')
    elif c.get("decided_by"):
        verb = "Rejected" if c["status"] == "rejected" else "Approved"
        sig = (c.get("approval") or {}).get("approval_digest", "")
        when = (c.get("decided_at") or "")[11:16]
        out = (f'<div class="decided">{verb} by <b>{e(who_label(c["decided_by"], c.get("auth_method")))}</b>'
               f' · {e(when)} UTC' + (f' · approval HMAC <span class="mono">{e(sig[:12])}…</span>' if sig else "") + "</div>")
    if c.get("paid_by"):
        out += (f'<div class="decided">Paid (second signature) by '
                f'<b>{e(who_label(c["paid_by"], c.get("paid_auth_method")))}</b></div>')
    return out


def _pp_ref(r: dict) -> str:
    rid = r.get("id") or (r.get("batch_header") or {}).get("payout_batch_id") or ""
    st = r.get("status") or (r.get("batch_header") or {}).get("batch_status") or ""
    return " · ".join(x for x in (f"PayPal {rid}" if rid else "", PAYPAL_STATUS.get(st, st.lower().replace("_", " "))) if x)


def _when(iso: str) -> str:
    """'2026-11-12T22:21:05+00:00' -> 'Nov 12, 22:21'."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%b %-d, %H:%M")
    except ValueError:
        return iso[:16].replace("T", " ")


def _drafted_by(planner: str) -> str:
    lab = agent_label(planner or "")
    return "the demo agent" if lab == DEMO_AGENT else lab.removeprefix("Agent: ")


def _card(c: dict, state: dict, csrf: str, user: str | None) -> str:
    allc = c["verification"] + c["rule_checks"]
    failed = [x for x in allc if not x["ok"]]
    codes = {x["rule"].split(" ", 1)[0] for x in failed}
    payees = state["policy"].get("payees") or {}
    action = ACTION_LABEL.get(c["action"], c["action"])
    tag = ' · <span class="tag">Agentic commerce</span>' if c["action"] == "purchase" else ""
    label = f'<h3>{e(action)}{tag} · from message “{e(c["source_subject"])}”</h3>'
    amount = money(c["amount"], c["currency"])
    r = c.get("result") or {}
    invalid = c["id"] in set(state.get("approval_invalid") or ())
    awaiting = c["status"] == "executed" and c["action"] == "purchase" and r.get("status") != "COMPLETED"
    hidden = (f'<input type="hidden" name="csrf" value="{e(csrf)}"><input type="hidden" name="card" value="{e(c["id"])}">')

    if c["status"] in ("executed", "rejected", "expired") and not awaiting:
        if c["status"] == "executed":
            what = DONE_VERB.get(c["action"], "sent to PayPal")
            right = _decided(c, invalid) + f'<div class="decided mono">{e(_pp_ref(r))}</div>'
        else:
            what = c["status"]
            right = _decided(c, invalid)
        if state["paypal_mode"] == "mock" and c["status"] == "executed":
            what += " (offline demo)"
        return (f'<article class="box card done" id="{e(c["id"])}"><div>{label}<p class="amt">{e(amount)} '
                f'<span>{e(what)}</span></p></div><div class="right">{right}</div></article>')

    if c["status"] == "pending":
        chip = '<span class="st">Ready to sign</span>'
    elif c["status"] == "blocked":
        chip = '<span class="st blocked"><span class="dot"></span>Blocked by code</span>'
    elif awaiting:
        chip = '<span class="st">Waiting for payment</span>'
    else:
        chip = f'<span class="st quiet">{e(c["status"])}</span>'

    if c["status"] == "blocked":
        pol = state["policy"]
        limit = (pol.get("max_per_action") or {}).get(c["action"])
        middle = (f'<div class="reason"><p>Countersign stopped this before it reached you.</p>'
                  f'<p>{e(_reason(failed, _num(limit, c["currency"]) if limit else "", _num(pol.get("daily_outflow_cap", ""), c["currency"]) if pol.get("daily_outflow_cap") else ""))}</p></div>')
    else:
        middle = f'<div class="why"><p>Why the agent proposes this</p><p>{e(c["rationale"])}</p></div>'
    middle += _checks(c)

    if awaiting:
        link = r.get("approve_url") or ""
        go = (f'<a href="{e(link)}" target="_blank" rel="noopener">Open the PayPal checkout</a>, pay, then capture here.'
              if link.startswith("https://") else "Mock mode has no PayPal page; the button simulates the payment.")
        label_btn = "Simulate paying at PayPal and capture" if state["paypal_mode"] == "mock" else "I paid at PayPal: capture"
        middle += (f'<div class="paybox"><div><p><b>Second signature.</b> PayPal order <span class="mono">{e(r.get("id"))}</span> '
                   f'is created and is waiting for payment. Pay it as the shop owner.</p><p class="small mute">{go}</p></div>'
                   f'<form class="inline" method="post" action="/pay">{hidden}'
                   f'<button class="approve">{label_btn}</button></form></div>')
    if c.get("error"):
        middle += f'<div class="err"><b>PayPal error:</b> {e(c["error"])}</div>'
    if c["status"] not in ("pending", "blocked"):
        middle += _decided(c, invalid)
    exp = _when(c.get("expires_at") or "")
    if c["status"] in ("pending", "blocked") and exp:
        middle += f'<p class="meta">Drafted by {e(_drafted_by(c["planner"]))} · open until {e(exp)} UTC</p>'
    elif c["status"] not in ("pending", "blocked"):
        middle += f'<p class="meta">Drafted by {e(_drafted_by(c["planner"]))}</p>'

    foot = ""
    if c["status"] == "pending":
        sig = (f'You sign as <b>{e(user)}</b> (authenticated)' if user else "Sign in to approve")
        foot = (f'<div class="foot"><span class="sig">{sig} · signature bound to digest '
                f'<span class="mono">{e(c["digest"][:8])}…</span></span><div class="btns">'
                f'<form class="inline" method="post" action="/reject">{hidden}<button class="reject">Reject</button></form>'
                f'<form class="inline" method="post" action="/approve">{hidden}'
                f'<input type="hidden" name="digest" value="{e(c["digest"])}">'
                f'<button class="approve">{e(APPROVE_VERB.get(c["action"], "Approve"))} {e(amount)}</button></form></div></div>')
    elif c["status"] == "blocked":
        foot = (f'<div class="foot"><span class="sig">Approve is disabled for blocked actions.</span><div class="btns">'
                f'<form class="inline" method="post" action="/reject">{hidden}<button class="reject">Reject</button></form>'
                f'<button disabled title="Blocked by code-enforced rules">Approve (blocked)</button></div></div>')
    return (f'<article class="box card" id="{e(c["id"])}"><div class="body"><div class="top"><div>{label}'
            f'<p class="amt">{e(amount)}</p><p class="cp">{_counterparty(c, payees, codes)}</p></div>{chip}</div>'
            f'{middle}</div>{foot}</article>')


def _aside(state: dict) -> str:
    pol = state["policy"]
    currencies = pol.get("currencies") or ["USD"]
    cur = "USD" if "USD" in currencies else currencies[0]
    try:
        pct = max(0, min(100, int(100 * Decimal(state["outflow_today"]) / Decimal(pol["daily_outflow_cap"]))))
    except (InvalidOperation, ZeroDivisionError, KeyError):
        pct = 0
    limits = "".join(f'<div class="kv"><span>{e(ACTION_LABEL.get(k, k))} (per action)</span><b>≤ {e(money(v, cur, True))}</b></div>'
                     for k, v in pol["max_per_action"].items())
    payees = pol.get("payees") or {}
    payee_txt = ", ".join(f"{v}" for v in payees.values()) if isinstance(payees, dict) else ", ".join(payees)
    cat = pol.get("catalog") or {}
    head = (state["audit"][-1]["hash"][:8] if state.get("audit") else "")
    if state["audit_ok"]:
        chain = (f'<p class="chain"><span class="dot ink"></span>Verified · {state.get("audit_count", len(state["audit"]))} entries</p>'
                 + (f'<p class="mono mute" style="margin-top:4px">head {e(head)}…</p>' if head else ""))
    else:
        chain = f'<p class="chain"><span class="dot"></span><b>Broken</b></p><p class="small">{e(state["audit_msg"])}</p>'
    return f"""<aside>
<section class="panel"><p class="eyebrow">Today's outflow</p>
<p class="big">{e(money(state["outflow_today"], cur))} <span>of {e(money(pol["daily_outflow_cap"], cur, True))} cap</span></p>
<div class="meter"><i style="width:{pct}%"></i></div></section>
<section class="panel" id="rules"><p class="eyebrow">Rules in force</p>{limits}
<div class="kv"><span>Daily outflow</span><b>≤ {e(money(pol["daily_outflow_cap"], cur, True))}</b></div>
<div class="kv"><span>Vetted payees</span><b>{len(payees)}</b></div>
<div class="kv"><span>Supplier catalogue</span><b>{len(cat)} SKUs</b></div>
<p class="note">Payees: {e(payee_txt)}. SKUs: {e(", ".join(cat))}.</p>
<p class="note">Rules run in code (countersign/policy.py) before a card reaches you, and again just before PayPal is
called. The agent cannot change them.</p></section>
<section class="panel plain"><p class="eyebrow">Audit chain</p>{chain}
<p class="note"><a href="/audit">Open the audit log</a></p></section>
</aside>"""


def render(state: dict, csrf: str, flash: str = "", user: str | None = None) -> str:
    order = {m["id"]: i for i, m in enumerate(state["inbox"])}
    def awaiting(c):
        return c["status"] == "executed" and c["action"] == "purchase" and (c.get("result") or {}).get("status") != "COMPLETED"
    cards = sorted(state["cards"], key=lambda c: (0.5 if awaiting(c) else STATUS_ORDER.get(c["status"], 9),
                                                  order.get(c["source_id"], 99)))
    cards_html = "".join(_card(c, state, csrf, user) for c in cards)
    notes_html = "".join(f'<p class="skip">No action on {e(n.get("source_id"))}: {e(n.get("reason"))}</p>'
                         for n in state["notes"])
    n = lambda st: sum(c["status"] == st for c in state["cards"])  # noqa: E731
    waiting = n("pending") + sum(awaiting(c) for c in state["cards"])
    done = sum(c["status"] in ("executed", "rejected", "expired", "failed") and not awaiting(c) for c in state["cards"])
    stopped = sum(Decimal(str(c["amount"])) for c in state["cards"] if c["status"] == "blocked")
    summary = ""
    if state["cards"]:
        tabs = [("Waiting", waiting), ("Blocked", n("blocked")), ("Done", done)]
        sel = next((i for i, (_, k) in enumerate(tabs) if k), 0)
        summary = ('<div class="summary">' + "".join(
            f'<span{" class=\"on\"" if i == sel else ""}>{t} · {k}</span>' for i, (t, k) in enumerate(tabs)) + '</div>')
        intro = (f'The agent read {len(state["inbox"])} messages and drafted {len(state["cards"])} actions'
                 + (f'; code blocked {n("blocked")} ({e(money(stopped, "USD"))} stopped)' if n("blocked") else "")
                 + ". Nothing moves money until a signed-in approver signs.")
    else:
        intro = "Nothing moves money until a signed-in approver signs."
    flash_html = f'<div class="flash">{e(flash)}</div>' if flash else ""
    inbox_html = "".join(f'<div class="msg"><p class="from">{e(m["from"])} · {e(m["id"])}</p><p><b>{e(m["subject"])}</b></p>'
                         f'<p>{e(m["body"])}</p></div>' for m in state["inbox"])
    empty = ('<div class="empty">No decisions yet. Run the agent: it reads the inbox below and drafts one card per '
             'message.</div>')
    body = f"""{_head(user, csrf, state, "decisions")}<main>{_aside(state)}
<section class="col">{flash_html}
<div class="titlerow"><h1>Decisions</h1><div class="actions">
<form class="inline" method="post" action="/reset"><input type="hidden" name="csrf" value="{e(csrf)}"><button>Reset demo</button></form>
<form class="inline" method="post" action="/run"><input type="hidden" name="csrf" value="{e(csrf)}"><button class="primary">Run agent on inbox</button></form>
</div></div>
<div class="titlerow">{summary}<p class="intro">{intro}</p></div>
{notes_html}{cards_html or empty}
<h2 class="sec">Inbox</h2><p class="intro" style="margin-top:-10px">The messages the agent reads. It can only look up and propose.</p>
<div class="inbox">{inbox_html}</div>
</section></main>"""
    return _page("Countersign", body)


EVENT_LABEL = {"card_approved": "Signed", "card_created": "Proposed", "executed": "PayPal call",
               "execution_failed": "PayPal error", "card_rejected": "Rejected", "card_expired": "Expired",
               "approval_refused": "Refused", "agent_run_started": "Agent run", "agent_run_finished": "Agent done",
               "agent_no_action": "No action", "login": "Signed in", "logout": "Signed out", "login_failed": "Sign-in refused",
               "payer_approval_simulated": "Payment simulated", "purchase_paid": "Paid", "demo_reset": "Demo reset",
               "purchase_capture_failed": "Capture failed"}


AUTH_TXT = {AUTH_METHOD: "authenticated session"}


def _how(method: str | None) -> str:
    return AUTH_TXT.get(method or "", f"not authenticated ({method or 'name typed'})")


def _audit_detail(a: dict, cards: dict | None = None) -> str:
    """One plain-English line per audit event. Display only: the signed audit data itself is never changed."""
    d = a.get("details") or {}
    ev = a.get("event")
    card = (cards or {}).get(d.get("card")) or {}
    action = d.get("action") or card.get("action") or ""
    noun = ACTION_LABEL.get(action, "Action")
    amt = money(d["amount"], d.get("currency", "")) if d.get("amount") else (
        money(card["amount"], card.get("currency", "")) if card.get("amount") else "")
    what = f"{amt} {noun.lower()}".strip()
    mock = " (offline demo)" if d.get("paypal_mode") == "mock" else ""
    if ev == "agent_run_started":
        n = d.get("messages")
        return e(f"Agent started reading {n} inbox message{'' if n == 1 else 's'}" if n is not None else "Agent started")
    if ev == "agent_run_finished":
        n, b = int(d.get("proposals") or 0), int(d.get("blocked") or 0)
        s = f"Agent drafted {n} action{'' if n == 1 else 's'}"
        return e(s + (f", {b} blocked by code" if b else ", none blocked"))
    if ev == "agent_no_action":
        src = d.get("source_id")
        why = str(d.get("reason") or "nothing to do")
        return e(f"No action on {src}: {why}" if src else f"No action: {why}")
    if ev == "card_created":
        if d.get("status") == "blocked":
            fails = []
            for r in d.get("failed_checks") or []:
                lab = check_label(r, False)
                lab = lab[:1].lower() + lab[1:]
                if lab not in fails:
                    fails.append(lab)
            return e(f"{what} drafted · blocked by code: " + ", ".join(fails) if fails else f"{what} drafted · blocked by code")
        return e(f"{what} drafted · all checks passed, ready to sign")
    if ev == "card_approved":
        return (f'{e(noun)} approved · {e(_how(d.get("auth_method")))} · approval HMAC '
                f'<span class="mono">{e((d.get("approval_digest") or "")[:10])}…</span>')
    if ev == "card_rejected":
        note = f" · note: {d['note']}" if d.get("note") else ""
        return e(f"{what} rejected · {_how(d.get('auth_method'))}{note}")
    if ev == "card_expired":
        return e(f"{what} expired without a signature")
    if ev == "approval_refused":
        why = {"digest mismatch": "the card changed after it was shown",
               "re-check failed": "a rule failed when checked again"}.get(d.get("reason"), d.get("reason") or "refused")
        return e(f"Approval of {what} refused: {why}")
    if ev == "executed":
        st = d.get("paypal_status") or ""
        ref = {"refund": "refund", "payout": "payout batch", "invoice": "invoice", "purchase": "order"}.get(action, "id")
        parts = [what, f"PayPal {ref} {d.get('paypal_id')}" if d.get("paypal_id") else "",
                 PAYPAL_STATUS.get(st, st.lower().replace("_", " "))]
        return e(" · ".join(x for x in parts if x) + mock)
    if ev == "execution_failed":
        return e(f"PayPal refused {what}: {d.get('error', '')}")
    if ev == "payer_approval_simulated":
        return e(f"Demo mode: paying order {d.get('order')} at PayPal was simulated")
    if ev == "purchase_paid":
        return e(f"{what} paid and captured · order {d.get('order')}" + (f" · capture {d['capture']}" if d.get("capture") else ""))
    if ev == "purchase_capture_failed":
        return e(f"Capture failed at PayPal: {d.get('error', '')}")
    if ev == "login":
        return e("Signed in with password" if d.get("auth_method") == AUTH_METHOD else f"Signed in ({_how(d.get('auth_method'))})")
    if ev == "logout":
        return "Signed out"
    if ev == "login_failed":
        return e(f"Sign-in refused for {d.get('username') or 'unknown name'}")
    if ev == "demo_reset":
        return "Demo data reset; a new audit chain starts here"
    return e(" · ".join(f"{k.replace('_', ' ')} {v}" for k, v in d.items() if not isinstance(v, (dict, list)))[:160])


def render_audit(state: dict, csrf: str, user: str | None = None) -> str:
    rows = []
    cards = {c["id"]: c for c in state.get("cards") or []}
    for a in reversed(state["audit"]):
        ev, actor = a.get("event", ""), a.get("actor", "")
        d = a.get("details") or {}
        cls = "signed" if ev in ("card_approved", "purchase_paid") else (
            "blocked" if ev == "card_created" and d.get("status") == "blocked" else "")
        label = "Blocked" if cls == "blocked" else EVENT_LABEL.get(ev, ev)
        dot = '<span class="dot"></span>' if cls == "blocked" else ""
        chip = f'<span class="ev {cls}">{dot}{e(label)}</span>'
        who = (f'<span class="person"><span class="av">{e(initials(actor))}</span>{e(actor)}</span>'
               if actor not in HUMAN_ACTORS_NOT else e(ACTOR_LABEL.get(actor, actor.capitalize())))
        rows.append(f'<tr><td class="t">{e(a["ts"][11:19])}</td><td>{who}</td><td>{chip}</td>'
                    f'<td>{_audit_detail(a, cards)}</td><td class="h">{e(a["hash"][:8])}</td></tr>')
    if state["audit_ok"]:
        n = state.get("audit_count", len(state["audit"]))
        checked = datetime.now(timezone.utc).strftime("%H:%M")
        badge = (f'<div class="badge">{ICON_SHIELD}<div><b>Chain verified · {n} of {n} entries</b>'
                 f'<span class="small mute">checked {e(checked)} UTC</span></div></div>')
    else:
        badge = (f'<div class="badge bad"><span class="dot"></span><div><b>Chain broken: verification failed</b>'
                 f'<span class="small">{e(state["audit_msg"])}</span></div></div>')
    shown = f"newest {len(state['audit'])} entries" if len(state["audit"]) >= 40 else "newest first"
    body = f"""{_head(user, csrf, state, "audit")}<main style="flex-direction:column;gap:24px">
<div class="audit-head"><div><h1 style="font-size:30px;font-weight:600;letter-spacing:-.02em">Audit log</h1>
<p class="mute">Every proposal, check, signature and PayPal call ({shown}). Each entry is HMAC-signed with a server key
and includes the hash of the one before it, so editing any line breaks the chain.</p></div>{badge}</div>
<div class="tablewrap"><table><thead><tr><th scope="col">Time (UTC)</th><th scope="col">Who</th><th scope="col">Event</th>
<th scope="col">Detail</th><th scope="col">Hash</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>
</main>"""
    return _page("Countersign: audit log", body)


def make_handler(engine: Engine, auth: Auth):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        def _send(self, code: int, body: str | bytes, ctype: str = "text/html; charset=utf-8", extra=None,
                  cache: str = "no-store"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; font-src 'self'; "
                             "img-src 'self'; form-action 'self'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, msg: str, cookie: str | None = None, s=None):
            """303 to the main page. `msg` is a MESSAGES key, or free text kept in session `s` (shown once)."""
            if msg not in MESSAGES:
                if s is not None:
                    s.flash = msg
                msg = "flash" if s is not None else ""
            self.send_response(303)
            self.send_header("Location", "/?msg=" + msg if msg else "/")
            if cookie is not None:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()

        def _session(self):
            return auth.session(self.headers.get("Cookie"))

        def _login_page(self, msg: str = "", code: int = 200):
            return self._send(code, render_login(auth.login_token(), msg, bool(auth.approvers.names())))

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/healthz":  # no data, used by scripts to wait for the server
                st = engine.state()
                return self._send(200, json.dumps({"paypal_mode": st["paypal_mode"], "planner": st["planner"],
                                                   "cards": len(st["cards"])}), "application/json")
            if url.path.startswith("/static/"):  # fonts + logo only, from a fixed list (no path from the URL is opened)
                name = url.path[len("/static/"):]
                if name not in STATIC_FILES:
                    return self._send(404, "not found", "text/plain")
                return self._send(200, (STATIC / name).read_bytes(), STATIC_FILES[name], cache="public, max-age=86400")
            s = self._session()
            key = urllib.parse.parse_qs(url.query).get("msg", [""])[0]
            msg = MESSAGES.get(key, "")
            if key == "flash" and s:
                msg, s.flash = s.flash, ""
            if url.path == "/api/state":
                if not s:
                    return self._send(401, json.dumps({"error": "sign in first"}), "application/json")
                return self._send(200, json.dumps(engine.state(), default=str), "application/json")
            if url.path not in ("/", "/audit"):
                return self._send(404, "not found", "text/plain")
            if not s:
                return self._login_page(msg)
            if url.path == "/audit":
                return self._send(200, render_audit(engine.state(), s.csrf, s.user))
            return self._send(200, render(engine.state(), s.csrf, msg, s.user))

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, "bad Content-Length", "text/plain")
            if n < 0:
                return self._send(400, "bad Content-Length", "text/plain")
            raw = self.rfile.read(min(n, 65536)).decode("utf-8", "replace")  # invalid UTF-8 -> U+FFFD, never a traceback
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
            path = urllib.parse.urlsplit(self.path).path
            if path == "/login":
                if not auth.check_login_token(form.get("login_token", "")):
                    return self._login_page("The sign-in form expired; please try again.", 403)
                try:
                    s = auth.login(form.get("username", ""), form.get("password", ""), client=self.client_address[0])
                except AuthError as err:
                    engine.audit.record("login_failed", "system", username=auth.audit_name(form.get("username", "")),
                                        reason=str(err))
                    return self._login_page(f"Sign-in refused: {err}.", 401)
                engine.audit.record("login", s.user, auth_method=s.auth_method)
                return self._redirect("signed_in", f"{COOKIE}={auth.cookie_value(s)}; HttpOnly; "
                                      f"SameSite=Strict; Path=/; Max-Age={SESSION_TTL}")
            s = self._session()
            if not s:
                return self._send(403, "sign in first", "text/plain")
            if not auth.csrf_ok(s, form.get("csrf", "")):
                return self._send(403, "bad csrf token", "text/plain")
            who, how = s.user, s.auth_method  # identity comes only from the session; any "who"/"name" field is ignored
            try:
                if path == "/logout":
                    auth.logout(s)
                    engine.audit.record("logout", who)
                    return self._redirect("signed_out", f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
                if path == "/run":
                    cards = engine.run_agent()
                    blocked = sum(c["status"] == "blocked" for c in cards)
                    return self._redirect(f"Agent drafted {len(cards)} action{'' if len(cards) == 1 else 's'}; "
                                          f"{blocked} blocked by code.",
                                          s=s)
                if path == "/approve":
                    c = engine.approve(form.get("card", ""), who, form.get("digest", ""), auth_method=how)
                    return self._redirect(f"{c['id']}: {c['status']}, approved by {who_label(who, how)}"
                                          + (f" ({c['error']})" if c.get("error") else ""), s=s)
                if path == "/pay":
                    c = engine.complete_purchase(form.get("card", ""), who, auth_method=how)
                    return self._redirect(f"{c['id']}: purchase paid, PayPal order {c['result'].get('status')}", s=s)
                if path == "/reject":
                    c = engine.reject(form.get("card", ""), who, auth_method=how)
                    return self._redirect(f"{c['id']}: rejected by {who_label(who, how)}", s=s)
                if path == "/reset":
                    engine.reset(who)
                    return self._redirect("reset")
            except DecisionError as err:
                return self._redirect(f"Refused: {err}", s=s)
            return self._send(404, "not found", "text/plain")

    return Handler


def make_server(engine: Engine, port: int = 8765, auth: Auth | None = None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(engine, auth or Auth(engine.data_dir)))


def serve(engine: Engine, port: int = 8765) -> None:
    auth = Auth(engine.data_dir)
    httpd = make_server(engine, port, auth)
    print(f"Countersign running at http://127.0.0.1:{port}  (PayPal: {engine.paypal.mode}, agent: {engine.planner.name})")
    names = auth.approvers.names()
    if names:
        print(f"Approver accounts: {', '.join(names)}. Sign in at the page above.")
    else:
        print("No approver accounts yet. Nobody can approve until you create one:\n"
              "  python3 -m countersign add-approver <name>")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
