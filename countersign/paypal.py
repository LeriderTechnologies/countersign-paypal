"""PayPal client layer.

Two backends share one interface:
  * SandboxPayPal: real HTTPS calls to the PayPal REST *sandbox* (api-m.sandbox.paypal.com).
    The host is hard-asserted on every request; there is no code path to the live API.
  * MockPayPal: an offline stand-in that returns PayPal-shaped JSON and the same error
    names (e.g. REFUND_AMOUNT_EXCEEDED). Used whenever sandbox credentials are absent.

APIs used: OAuth2 client-credentials, Payments v2 (captures, refunds), Payouts v1, Invoicing v2,
Orders v2 (agent-proposed supplier purchases, and seeding sandbox demo data).
"""
from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal
from pathlib import Path

from .config import SANDBOX_BASE_URL, SANDBOX_HOST
from .money import fmt


class PayPalError(RuntimeError):
    def __init__(self, status: int, name: str, message: str, debug_id: str = ""):
        super().__init__(f"{status} {name}: {message}")
        self.status, self.name, self.message, self.debug_id = status, name, message, debug_id


class SandboxOnlyViolation(RuntimeError):
    """Raised if any request would leave the PayPal sandbox host."""


def assert_sandbox_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != SANDBOX_HOST or parts.port not in (None, 443):
        raise SandboxOnlyViolation(f"refusing non-sandbox PayPal URL: {url}")


# --------------------------------------------------------------------------------------
# Real sandbox client
# --------------------------------------------------------------------------------------
class SandboxPayPal:
    mode = "sandbox"

    def __init__(self, client_id: str, client_secret: str, base_url: str = SANDBOX_BASE_URL, timeout: float = 30):
        assert_sandbox_url(base_url)
        if base_url.rstrip("/") != SANDBOX_BASE_URL:
            raise SandboxOnlyViolation(f"base URL must be exactly {SANDBOX_BASE_URL}")
        self.base_url = SANDBOX_BASE_URL
        self._cid, self._secret = client_id, client_secret
        self._token: str | None = None
        self._token_exp = 0.0
        self.timeout = timeout

    # -- transport ---------------------------------------------------------------------
    def _http(self, method: str, path: str, body=None, headers=None, form: str | None = None) -> dict:
        url = self.base_url + path
        assert_sandbox_url(url)
        hdrs = {"Accept": "application/json"}
        hdrs.update(headers or {})
        data = None
        if form is not None:
            data = form.encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as err:
            try:
                payload = json.loads(err.read() or b"{}")
            except ValueError:
                payload = {}
            name = payload.get("name") or payload.get("error") or "HTTP_ERROR"
            msg = payload.get("message") or payload.get("error_description") or str(err)
            details = payload.get("details") or []
            if details and isinstance(details, list) and isinstance(details[0], dict):
                name = details[0].get("issue", name)
                msg = details[0].get("description", msg)
            raise PayPalError(err.code, name, msg, payload.get("debug_id", "")) from None

    def _auth(self) -> dict:
        if not self._token or time.time() > self._token_exp - 60:
            basic = base64.b64encode(f"{self._cid}:{self._secret}".encode()).decode()
            tok = self._http("POST", "/v1/oauth2/token", headers={"Authorization": f"Basic {basic}"},
                             form="grant_type=client_credentials")
            self._token = tok["access_token"]
            self._token_exp = time.time() + int(tok.get("expires_in", 300))
        return {"Authorization": f"Bearer {self._token}"}

    def _call(self, method: str, path: str, body=None, request_id: str | None = None) -> dict:
        headers = self._auth()
        if request_id:
            headers["PayPal-Request-Id"] = request_id  # idempotency: retries never double-pay
        return self._http(method, path, body=body, headers=headers)

    # -- read --------------------------------------------------------------------------
    def get_capture(self, capture_id: str) -> dict:
        return self._call("GET", f"/v2/payments/captures/{urllib.parse.quote(capture_id)}")

    # -- money actions (only ever called by the Executor after human approval) ---------
    def refund_capture(self, capture_id: str, amount: Decimal, currency: str, note: str, request_id: str) -> dict:
        body = {"amount": {"value": fmt(amount), "currency_code": currency}, "note_to_payer": note[:255]}
        return self._call("POST", f"/v2/payments/captures/{urllib.parse.quote(capture_id)}/refund", body, request_id)

    def create_payout(self, receiver: str, amount: Decimal, currency: str, note: str, request_id: str) -> dict:
        body = {
            "sender_batch_header": {"sender_batch_id": request_id[:30], "email_subject": "You have a payout"},
            "items": [{
                "recipient_type": "EMAIL",
                "amount": {"value": fmt(amount), "currency": currency},
                "receiver": receiver,
                "note": note[:4000],
                "sender_item_id": request_id[:30],
            }],
        }
        return self._call("POST", "/v1/payments/payouts", body, request_id)

    def create_and_send_invoice(self, recipient: str, items: list[dict], currency: str, note: str,
                                request_id: str) -> dict:
        body = {
            "detail": {"currency_code": currency, "note": note[:4000]},
            "primary_recipients": [{"billing_info": {"email_address": recipient}}],
            "items": [{
                "name": it["name"][:200],
                "quantity": str(it["quantity"]),
                "unit_amount": {"currency_code": currency, "value": fmt(Decimal(str(it["unit_price"])))},
            } for it in items],
        }
        created = self._call("POST", "/v2/invoicing/invoices", body, request_id)
        inv_id = created.get("id") or created.get("href", "").rstrip("/").rsplit("/", 1)[-1]
        sent = self._call("POST", f"/v2/invoicing/invoices/{urllib.parse.quote(inv_id)}/send",
                          {"send_to_invoicer": False}, request_id + "-send")
        return {"id": inv_id, "status": "SENT", "send_response": sent}

    def create_purchase_order(self, supplier: str, items: list[dict], currency: str, note: str, request_id: str,
                              return_url: str = "http://127.0.0.1:8765/") -> dict:
        """Orders v2 checkout with the supplier as payee. The shop owner pays it at PayPal (payer-action link).

        UNTESTED against the real sandbox: no credentials yet. The supplier email must be a sandbox business account.
        """
        body = purchase_order_body(supplier, items, currency, note, request_id, return_url)
        order = self._call("POST", "/v2/checkout/orders", body, request_id)
        return summarize_order(order)

    def get_order(self, order_id: str) -> dict:
        return summarize_order(self._call("GET", f"/v2/checkout/orders/{urllib.parse.quote(order_id)}"))

    # -- sandbox demo-data helpers (buyer approval happens in the sandbox web UI) ------
    def create_order(self, amount: Decimal, currency: str, description: str) -> dict:
        body = {"intent": "CAPTURE", "purchase_units": [{
            "amount": {"currency_code": currency, "value": fmt(amount)}, "description": description[:127]}]}
        return self._call("POST", "/v2/checkout/orders", body, str(uuid.uuid4()))

    def capture_order(self, order_id: str, request_id: str | None = None) -> dict:
        return self._call("POST", f"/v2/checkout/orders/{urllib.parse.quote(order_id)}/capture", {},
                          request_id or str(uuid.uuid4()))


def purchase_order_body(supplier: str, items: list[dict], currency: str, note: str, request_id: str,
                        return_url: str) -> dict:
    total = sum((Decimal(str(i["unit_price"])) * int(i["quantity"]) for i in items), Decimal("0"))
    return {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": request_id[:256],
            "description": (note or "Countersign supplier purchase")[:127],
            "payee": {"email_address": supplier},
            "amount": {"currency_code": currency, "value": fmt(total),
                       "breakdown": {"item_total": {"currency_code": currency, "value": fmt(total)}}},
            "items": [{"name": it["name"][:127], "sku": str(it["sku"])[:127], "quantity": str(int(it["quantity"])),
                       "category": "PHYSICAL_GOODS",
                       "unit_amount": {"currency_code": currency, "value": fmt(Decimal(str(it["unit_price"])))}}
                      for it in items],
        }],
        "payment_source": {"paypal": {"experience_context": {
            "user_action": "PAY_NOW", "return_url": return_url, "cancel_url": return_url}}},
    }


# PayPal's public sandbox test Visa (developer.paypal.com card testing). Sandbox only, moves no money.
SANDBOX_TEST_CARD = {"number": "4005519200000004", "expiry": "2030-12", "name": "Sandbox Buyer", "security_code": "123"}


def seed_order_body(order_ref: str, amount: str, currency: str, card: bool = False) -> dict:
    """Orders v2 body for one demo customer order. card=True pays it at once with the sandbox test card,
    so PayPal returns a COMPLETED capture without a buyer logging in."""
    body = {"intent": "CAPTURE", "purchase_units": [{"custom_id": order_ref, "description": f"Kiln & Co order {order_ref}",
                                                    "amount": {"currency_code": currency, "value": amount}}]}
    if card:
        body["payment_source"] = {"card": dict(SANDBOX_TEST_CARD)}
    return body


def order_ref_of(capture: dict) -> str | None:
    """The shop's order reference on a PayPal capture. Seeding puts it in custom_id (invoice_id must be unique
    per PayPal account forever, so a re-seed with invoice_id fails with DUPLICATE_INVOICE_ID); the mock uses
    invoice_id."""
    return capture.get("custom_id") or capture.get("invoice_id")


def summarize_order(order: dict) -> dict:
    link = next((l["href"] for l in order.get("links", []) if l.get("rel") in ("payer-action", "approve")), None)
    out = {"id": order.get("id"), "status": order.get("status"), "approve_url": link}
    caps = [c for pu in order.get("purchase_units", []) for c in (pu.get("payments") or {}).get("captures", [])]
    if caps:
        out["capture_id"] = caps[0].get("id")
        out["capture_status"] = caps[0].get("status")
    return out


# --------------------------------------------------------------------------------------
# Offline mock
# --------------------------------------------------------------------------------------
DEMO_CAPTURES = {
    # Fictional shop data for the demo inbox. Amounts are what PayPal says was captured.
    "MOCKCAP-1042": {"amount": "38.00", "currency_code": "USD", "invoice_id": "A-1042", "payer": "lena@example.com"},
    "MOCKCAP-1043": {"amount": "120.00", "currency_code": "USD", "invoice_id": "A-1043", "payer": "tom@example.com"},
    "MOCKCAP-1044": {"amount": "64.50", "currency_code": "USD", "invoice_id": "A-1044", "payer": "sam@example.com"},
}


class MockPayPal:
    """In-memory PayPal look-alike, persisted to a JSON file so the web UI survives restarts."""
    mode = "mock"

    def __init__(self, state_path: Path | None = None):
        self.state_path = state_path
        self._lock = threading.Lock()
        self.calls: list[tuple[str, str]] = []
        self.state = self._load()

    def _fresh(self) -> dict:
        caps = {}
        for cid, c in DEMO_CAPTURES.items():
            caps[cid] = {"id": cid, "status": "COMPLETED", "amount": {"value": c["amount"],
                         "currency_code": c["currency_code"]}, "invoice_id": c["invoice_id"],
                         "payer_email": c["payer"], "refunds": []}
        return {"captures": caps, "payouts": {}, "invoices": {}, "orders": {}, "idempotency": {}}

    def _load(self) -> dict:
        if self.state_path and self.state_path.exists():
            state = json.loads(self.state_path.read_text())
            state.setdefault("orders", {})
            return state
        return self._fresh()

    def _save(self) -> None:
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self.state, indent=1))

    def reset(self) -> None:
        with self._lock:
            self.state = self._fresh()
            self._save()

    def _idem(self, request_id: str, fn):
        with self._lock:
            if request_id in self.state["idempotency"]:
                return self.state["idempotency"][request_id]  # same as PayPal: replay, don't re-execute
            result = fn()
            self.state["idempotency"][request_id] = result
            self._save()
            return result

    # -- read --------------------------------------------------------------------------
    def get_capture(self, capture_id: str) -> dict:
        self.calls.append(("GET", f"/v2/payments/captures/{capture_id}"))
        cap = self.state["captures"].get(capture_id)
        if not cap:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", f"capture {capture_id} not found")
        out = {k: v for k, v in cap.items() if k != "refunds"}
        return out

    def refunded_total(self, capture_id: str) -> Decimal:
        cap = self.state["captures"].get(capture_id) or {"refunds": []}
        return sum((Decimal(r["amount"]["value"]) for r in cap["refunds"]), Decimal("0"))

    # -- money actions -----------------------------------------------------------------
    def refund_capture(self, capture_id: str, amount: Decimal, currency: str, note: str, request_id: str) -> dict:
        self.calls.append(("POST", f"/v2/payments/captures/{capture_id}/refund"))

        def do():
            cap = self.state["captures"].get(capture_id)
            if not cap:
                raise PayPalError(404, "RESOURCE_NOT_FOUND", f"capture {capture_id} not found")
            if currency != cap["amount"]["currency_code"]:
                raise PayPalError(422, "CURRENCY_MISMATCH", "refund currency differs from capture")
            done = sum((Decimal(r["amount"]["value"]) for r in cap["refunds"]), Decimal("0"))
            if done + amount > Decimal(cap["amount"]["value"]):
                raise PayPalError(422, "REFUND_AMOUNT_EXCEEDED", "refund exceeds captured amount")
            ref = {"id": "MOCKREF-" + uuid.uuid4().hex[:10].upper(), "status": "COMPLETED",
                   "amount": {"value": fmt(amount), "currency_code": currency}, "note_to_payer": note}
            cap["refunds"].append(ref)
            cap["status"] = "REFUNDED" if done + amount == Decimal(cap["amount"]["value"]) else "PARTIALLY_REFUNDED"
            return ref
        return self._idem(request_id, do)

    def create_payout(self, receiver: str, amount: Decimal, currency: str, note: str, request_id: str) -> dict:
        self.calls.append(("POST", "/v1/payments/payouts"))

        def do():
            bid = "MOCKBATCH-" + uuid.uuid4().hex[:10].upper()
            res = {"batch_header": {"payout_batch_id": bid, "batch_status": "PENDING",
                                    "sender_batch_header": {"sender_batch_id": request_id[:30]}},
                   "receiver": receiver, "amount": {"value": fmt(amount), "currency": currency}}
            self.state["payouts"][bid] = res
            return res
        return self._idem(request_id, do)

    def create_and_send_invoice(self, recipient: str, items: list[dict], currency: str, note: str,
                                request_id: str) -> dict:
        self.calls.append(("POST", "/v2/invoicing/invoices"))

        def do():
            iid = "MOCKINV-" + uuid.uuid4().hex[:10].upper()
            total = sum((Decimal(str(i["unit_price"])) * Decimal(str(i["quantity"])) for i in items), Decimal("0"))
            res = {"id": iid, "status": "SENT", "recipient": recipient,
                   "amount": {"value": fmt(total), "currency_code": currency}}
            self.state["invoices"][iid] = res
            return res
        return self._idem(request_id, do)


    def create_purchase_order(self, supplier: str, items: list[dict], currency: str, note: str, request_id: str,
                              return_url: str = "http://127.0.0.1:8765/") -> dict:
        self.calls.append(("POST", "/v2/checkout/orders"))
        body = purchase_order_body(supplier, items, currency, note, request_id, return_url)

        def do():
            oid = "MOCKORDER-" + uuid.uuid4().hex[:10].upper()
            order = {"id": oid, "status": "PAYER_ACTION_REQUIRED", "purchase_units": body["purchase_units"],
                     "links": [{"rel": "payer-action", "href": f"mock://paypal/checkoutnow?token={oid}"}]}
            self.state["orders"][oid] = order
            return summarize_order(order)
        return self._idem(request_id, do)

    def get_order(self, order_id: str) -> dict:
        self.calls.append(("GET", f"/v2/checkout/orders/{order_id}"))
        order = self.state["orders"].get(order_id)
        if not order:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", f"order {order_id} not found")
        return summarize_order(order)

    def simulate_payer_approval(self, order_id: str) -> None:
        """Mock only: stands in for the shop owner logging in at PayPal and pressing Pay."""
        with self._lock:
            order = self.state["orders"].get(order_id)
            if not order:
                raise PayPalError(404, "RESOURCE_NOT_FOUND", f"order {order_id} not found")
            if order["status"] == "PAYER_ACTION_REQUIRED":
                order["status"] = "APPROVED"
                self._save()

    def capture_order(self, order_id: str, request_id: str | None = None) -> dict:
        self.calls.append(("POST", f"/v2/checkout/orders/{order_id}/capture"))

        def do():
            order = self.state["orders"].get(order_id)
            if not order:
                raise PayPalError(404, "RESOURCE_NOT_FOUND", f"order {order_id} not found")
            if order["status"] != "APPROVED":
                raise PayPalError(422, "ORDER_NOT_APPROVED", "payer has not yet approved the order")
            pu = order["purchase_units"][0]
            pu["payments"] = {"captures": [{"id": "MOCKCAP-" + uuid.uuid4().hex[:8].upper(), "status": "COMPLETED",
                                            "amount": pu["amount"]}]}
            order["status"] = "COMPLETED"
            return summarize_order(order)
        return self._idem(request_id or f"capture-{order_id}", do)


def make_paypal(settings) -> SandboxPayPal | MockPayPal:
    if settings.paypal_mode == "sandbox":
        return SandboxPayPal(settings.paypal_client_id, settings.paypal_client_secret)
    return MockPayPal(settings.data_dir / "mock_paypal.json")
