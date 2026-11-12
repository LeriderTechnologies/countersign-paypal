# Countersign

**The AI proposes. A human countersigns. Code holds the line.**

A PayPal agent for small online shops that does the back-office money work (refunds, contractor payouts,
invoices) and also **buys supplies on the shop's behalf** (agentic commerce, via a PayPal checkout order).
It never moves money by itself. A prompt-injected "CEO" email or an over-claimed refund gets **blocked by
Python code**, whatever the model says. The legitimate actions take one human click each.

```bash
python3 -m countersign demo            # 60-second headless run, no keys, no installs (Python 3.10+)
python3 -m countersign add-approver alice   # create your approver login (password prompted, stored as a scrypt hash)
python3 -m countersign serve --fresh   # same flow in the browser: http://127.0.0.1:8765, sign in as alice
```

```
inbox (untrusted) ─► AI agent ─► propose only ─► rules in code + claims checked against PayPal ─► decision card
                                                                                   │ blocked: cannot be approved
                                                                                   ▼ pending
                                     human signs (digest-bound) ─► re-check ─► PayPal sandbox (idempotent)
                                                                                   │
                                                          every step ─► hash-chained audit log
```

| In the 6-message demo inbox | What happens |
|---|---|
| Broken mug, photographer's fee, café wholesale order | Refund, payout, invoice: **pending**, one click each |
| Fake "CEO": pay 4,900 USD to a new supplier now | **Blocked**: over limit, over daily cap, payee not vetted |
| "Refund A-1043, I paid 150" (PayPal says 120) | **Blocked**: claim contradicts PayPal, over refundable |
| Stock bot: reorder 5 boxes of cup blanks | **Purchase** from a vetted supplier at catalogue price: approve → PayPal order → owner pays |

![screenshot](docs/screenshot.png)

## How it works

Countersign reads the shop inbox and drafts the PayPal action each message needs: a refund, a payout, an
invoice, or a supplier purchase. Each action becomes a **decision card** that a person approves or rejects.
Before the card reaches that person, code checks it in two ways:

- **Boundary rules**, enforced in Python and not in the prompt: per-action limits, a daily outflow cap, a
  human-vetted payee list, a human-vetted supplier catalogue (SKU, supplier, price, max quantity), refunds
  capped at what PayPal says is still refundable, and duplicate detection. A card that breaks a rule is
  **blocked**, the card says why in plain English, and the Approve button is disabled.
- **Verification** of what the agent *claims*. Countersign checks the order ↔ capture link and the captured
  amount against PayPal's own data, and checks that the agent's evidence quote, payee and SKUs appear in the
  source message.

Approving a card executes it once, against the PayPal sandbox. The card id is the `PayPal-Request-Id`, so a
retry can never pay twice. A purchase gets **two signatures**: approving the card creates a PayPal Orders v2
checkout with the supplier as payee, and the owner then pays it at PayPal (in mock mode this step is
simulated and logged as simulated). Every step is written to a **hash-chained audit log** whose entries are
HMAC-signed with a server key (`data/audit.key`), so editing a past entry breaks verification visibly, even
if the editor re-hashes the rest of the chain (but see Limits: whoever holds the key file can re-sign).

## Why

Payment agents are coming ("agentic commerce"): agents that buy, refund and pay on a business's behalf.
Small merchants still won't hand an LLM their PayPal account.
Prompt rules alone ("never refund more than…") can be argued around. A fake "CEO" email or a customer who
misstates what they paid can push a model into a bad payment. Countersign keeps what the model is good at
(reading messy messages, drafting the right action) and moves the safety-critical decisions into code and a
human signature.

## Quick start (no accounts, no keys: about 1 minute)

Needs Python 3.10+ and nothing else. The core uses only the standard library.

```bash
git clone <this repo> countersign && cd countersign
python3 -m unittest -v                  # 62 tests
python3 -m countersign demo             # narrated headless end-to-end run (a scripted reviewer, labelled as not authenticated)
python3 -m countersign add-approver alice   # approver account: prompts for a password (12+ chars) twice
python3 -m countersign serve --fresh    # web UI at http://127.0.0.1:8765, agent already run; sign in as alice
python3 -m countersign verify-audit     # re-walk the audit hash chain and check its HMACs
```

### Approver accounts (who may sign)

Approving, rejecting and paying require a signed-in approver. Accounts are local and created on the command
line, never in the browser: `add-approver <name>` (prompted password, or `--password-stdin` for scripts),
`list-approvers`, `remove-approver <name>`. They are stored in `data/approvers.json` (gitignored, mode 600) as
salted scrypt hashes; no password is in the repo. The web UI then works like this:

- Sign-in form → a server-side session. The browser holds only a random session id signed with HMAC-SHA256
  (secret generated at first start in `data/session.key`), in an `HttpOnly; SameSite=Strict` cookie, 8 hours.
- Every POST needs the session and that session's CSRF token; otherwise 403. Logout ends the session.
- Failed logins wait 1 second each, and password checks run one at a time across all requests (a global
  gate, so parallel guesses are not faster). 5 failures in 15 minutes lock that account, 20 lock that client
  address, each for 15 minutes; the failure store is capped at 1,000 entries. Password and token checks are
  constant-time and never crash on non-ASCII input. Passwords use scrypt N=2^17, r=8, p=1 (about 0.2 s).
- `remove-approver` ends that approver's open sessions at their next request.
- A failed sign-in is logged with the name only if it is an existing account; anything else is logged as
  `unknown user #<keyed hash>`, so a password typed into the name field never reaches the log.
- Page messages come from a fixed list or from the session; free text in `?msg=` is not shown.
- **The approver's name comes only from the session.** There is no name field; a `who`/`name` field sent by a
  client is ignored (tested). The card then reads "Approved by alice (authenticated)".
- The approval computes an **approval digest**: HMAC-SHA256 under a server key over the card id, the digest of
  exactly what was shown, the approver, the auth method (`password`) and the time. It is stored on the card and
  in the `card_approved` audit entry. Every audit entry also carries an HMAC-SHA256 of its chain hash under the
  same key. The key is `data/audit.key`: 32 random bytes created with mode 600 on first run, gitignored, never
  committed. `verify-audit` re-walks the chain and checks every HMAC, so rewriting who approved fails
  verification even if the editor recomputes every later hash (tested). Entries without a valid HMAC fail.
  (A log written by a version before the HMACs therefore fails verification: move the old `data/audit.jsonl` aside.)
  The page re-checks each card's approval digest when it shows the card: an approver edited in `data/cards.json`
  shows as "Approval signature INVALID", not as a name (tested).
  **Limit:** this protects against someone who can edit the log but cannot read the key. Whoever holds
  `data/audit.key` can re-sign a rewritten log; a real deployment keeps the key in a secrets store or HSM, or
  anchors the chain head outside the machine.

Without credentials, Countersign uses a **built-in mock PayPal** (PayPal-shaped JSON and error names) and a
**deterministic offline planner**, so judges can run the full flow offline.

### Seeing what Claude decides, without a key

When Claude is the planner, every run's tool calls are saved to `data/claude_run.json` (fictional demo data
only, no keys). Copied to `docs/claude_run.json`, that run can be replayed offline:
`python3 -m countersign demo --replay` or `serve --fresh --replay`. Replay goes through exactly the same
rules and verification as a live run, so an edited recording is still blocked (tested).
**A recorded run is committed** (`docs/claude_run.json`, Claude Opus 5.5, 2026-11-12, mock PayPal): Claude refused
the CEO-fraud mail on its own and capped the A-1043 refund at the 120.00 USD PayPal actually captured. Without
the file, `--replay` falls back to the offline planner and says so.

### With the PayPal sandbox and Claude

```bash
cp .env.example .env && chmod 600 .env
# fill PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET (a *sandbox* REST app from developer.paypal.com)
# optional: ANTHROPIC_API_KEY (+ pip install anthropic) to use Claude as the planner
python3 -m countersign sandbox-seed --card             # sandbox orders A-1042..A-1044, paid with PayPal's sandbox test card
#   (without --card it prints approve links: approve each with your sandbox *personal* account, then
#    python3 -m countersign sandbox-capture A-1042 <ORDER_ID>, and the same for A-1043, A-1044)
python3 -m countersign add-approver alice             # once
python3 -m countersign serve --fresh
```

For the purchase demo in sandbox mode, set `COUNTERSIGN_SUPPLIER_EMAIL=` in `.env` to one of your sandbox
*business* accounts (Sandbox Accounts page): the buyer can only pay a payee PayPal knows.
**Verified against the real PayPal sandbox on 2026-11-07:** seeding, refund of a capture, payout (batch
PENDING; `example.com` receivers end UNCLAIMED, as expected), invoice create + send, and creating the purchase
order (PAYER_ACTION_REQUIRED with a checkout link). Paying that checkout needs a human logged in as the
sandbox buyer. The demo shop uses USD because a default sandbox business account holds USD: EUR payouts
failed with INSUFFICIENT_FUNDS.

Countersign **only ever talks to `https://api-m.sandbox.paypal.com`**. That host is asserted on every
request. `PAYPAL_MODE=live` or any other `PAYPAL_BASE_URL` stops the app at startup. Tests cover both.

## The demo inbox (and using your own)

The demo uses a built-in sample inbox for a fictional ceramics shop, "Kiln & Co" (`countersign/inbox.py`).
All names and emails are invented. To run on your own messages, export them from your mail or helpdesk to a
JSON list of `{"id", "from", "subject", "body"}` (example: `docs/inbox.example.json`) and start
`python3 -m countersign serve --inbox messages.json`. There is no live mail connector yet: the file is the
interface. Use Claude as the agent for real messages; the offline planner only understands the demo's patterns.

| Message | Agent proposes | Outcome |
|---|---|---|
| Broken mug, order A-1042, refund 38.00 USD | Refund 38.00 on the A-1042 capture | Pending → human approves → refunded |
| Photographer: fee 450.00 USD | Payout 450.00 to a vetted payee | Pending → approve → payout batch |
| Café: 24 cups at 18.00 USD, send invoice | Invoice 432.00 USD | Pending → approve → invoice sent |
| "CEO": pay a new supplier 4,900 USD now, skip checks | Payout 4,900 to an unknown email | **Blocked**: over the limit, over the daily cap, payee not vetted |
| "Refund A-1043, I paid 150.00 USD" | Refund 150.00 | **Blocked**: PayPal says 120.00 was captured (verification + refund cap) |
| Stock bot: BLANK-ESP-6 below reorder point, reorder 5 boxes | Purchase 5 × 21.00 = 105.00 USD from Clayworks Supply | Pending → approve → PayPal order (PAYER_ACTION_REQUIRED) → owner pays → captured |

The offline planner is naive on purpose: it believes the customer. This shows that safety does not depend on
the model behaving well. Claude, as the planner, is told to flag pressure and fraud and may call `no_action`. The code rules apply
either way.

## Architecture

```mermaid
flowchart LR
  I[Inbox<br/>untrusted text] --> P[Planner<br/>Claude or offline mock]
  P -- read-only --> L[lookup_order /<br/>vetted payees /<br/>supplier catalogue]
  L -- GET capture --> PP[(PayPal REST<br/>sandbox or mock)]
  P -- propose_* only --> E[Engine]
  E --> R[Boundary rules<br/>policy.py]
  E --> V[Verifier<br/>verify.py]
  R & V --> C[Decision card<br/>pending / blocked]
  C --> H{Human in<br/>web UI}
  H -- reject --> X[rejected]
  H -- approve + digest --> RC[Re-check rules + claims<br/>with fresh PayPal facts]
  RC -- pass --> EX[Executor<br/>PayPal-Request-Id = card id]
  EX --> PP
  EX -- purchase --> O[Orders v2 checkout<br/>owner pays at PayPal] --> PP
  E & H & EX --> A[(Hash-chained<br/>audit log)]
```

Plain-text version:

```
inbox ─► planner (Claude | mock) ──read-only──► PayPal GET (sandbox | mock)
              │ propose_* (no execute tool exists)
              ▼
        engine ─► rules (code) + verifier (claims vs PayPal) ─► decision card
                                                                     │
                                        human: approve (digest) / reject
                                                                     │
                     re-check at time of use ─► executor ─► PayPal POST (idempotent)
        every step ─► audit.jsonl (SHA-256 chain)
```

| File | Role |
|---|---|
| `countersign/planner.py` | Agent. Claude tool-use loop (`lookup_order`, `list_vetted_payees`, `list_supplier_catalog`, `propose_refund/payout/invoice/purchase`, `no_action`), a replay of a recorded Claude run, or the offline mock. No tool can move money. |
| `countersign/policy.py` | Boundary rules R1–R10, in plain Python (R10: vetted supplier catalogue). |
| `countersign/verify.py` | Claim checks V1–V6: evidence quote is verbatim, capture ↔ order, captured amount, counterparty email, SKUs named in the message. |
| `countersign/engine.py` | Card lifecycle, approval (digest + authenticated approver + re-check), execution, expiry. |
| `countersign/auth.py` | Approver accounts (salted scrypt), server-side sessions, CSRF tokens, failed-login throttling (account, client, global gate). |
| `countersign/inbox.py` | The built-in sample inbox, and `load_inbox()` for `serve --inbox FILE.json`. |
| `countersign/paypal.py` | `SandboxPayPal` (OAuth2, Payments v2 refunds, Payouts v1, Invoicing v2, Orders v2 for purchases and seeding) and `MockPayPal`. |
| `countersign/audit.py` | Append-only JSONL with a SHA-256 chain, HMAC-SHA256 per entry and per approval (server key `data/audit.key`), plus `verify-audit`. |
| `countersign/web.py` | Server-rendered approval UI on 127.0.0.1. Approver sign-in, per-session CSRF token, strict CSP, no JavaScript. |

## Guarantees and how they are tested

| Guarantee | Test |
|---|---|
| Only the sandbox host can be called, look-alike hosts included | `SandboxOnly.*` |
| Nothing is POSTed to PayPal before a human approves | `test_nothing_executes_without_approval`, `ClaudeLoop` |
| Blocked cards cannot be approved | `test_fraud_payout_blocked_by_code` |
| Wrong agent claims are caught | `test_verification_catches_wrong_claim` |
| A card edited after display is refused | `test_tampered_card_refused`, `test_stale_digest_refused` |
| "agent"/"system"/empty cannot approve | `test_agent_cannot_approve` |
| Executes once; duplicates are blocked | `test_approve_executes_once`, `test_duplicate_blocked` |
| Rules are re-checked at execution time | `test_recheck_at_execution_time`, `test_daily_cap` |
| Audit tampering is detected | `test_audit_chain_and_tamper_detection` |
| Purchases only from the vetted catalogue, at catalogue price and quantity | `AgenticCommerce.test_inflated_price_blocked`, `test_unknown_supplier_or_sku_blocked`, `test_quantity_and_limit` |
| A purchase needs card approval *and* payer approval; it is paid once | `test_approve_creates_order_then_owner_pays`, `test_sandbox_capture_refused_until_payer_approves` |
| A replayed (or edited) Claude run is checked like a live one | `ClaudeLoop.test_replay_still_enforces_rules` |
| No session → no approve/reject/pay/run (403), no state | `AuthenticatedApprover.test_no_session_is_refused` |
| Missing or wrong CSRF token → 403 | `test_bad_or_missing_csrf_is_refused` |
| Wrong password refused, repeated failures lock the account | `test_wrong_password_and_lockout` |
| The approver comes from the session; a `who`/`name` form field is ignored; identity + auth method are in the approval digest and audit entry | `test_identity_from_session_not_form_and_in_digest_and_audit`, `test_reject_and_pay_use_session_identity` |
| Editing who approved breaks `verify-audit` | `test_tampered_approver_breaks_chain` |
| Rewriting who approved **and recomputing every later hash** fails without `data/audit.key` (kept HMACs, guessed key, or stripped HMACs); with the key it can be re-signed (the documented limit) | `test_rewriting_whole_tail_without_key_fails` |
| The audit key is created mode 600 and reused after a restart | `test_audit_key_is_private_and_reused` |
| Non-ASCII cookie, CSRF or login token is refused, never a crash | `test_non_ascii_tokens_do_not_crash` |
| Per-client lockout, a capped failure store, and one password check at a time | `test_client_and_global_throttle_and_capped_store`, `test_global_gate_serialises_password_checks` |
| A removed approver's session stops working | `test_removed_approver_loses_session` |
| A failed login never logs a typed secret | `test_failed_login_does_not_log_typed_secret` |
| `?msg=` cannot inject page text | `test_msg_param_only_shows_fixed_messages` |
| Your own inbox file loads and is validated | `OwnInbox.test_load_inbox_and_run`, `test_bad_inbox_files_refused` |
| Passwords stored only as salted scrypt hashes, file mode 600 | `test_password_hash_is_salted_scrypt`, `test_account_rules_and_cli` |

## How it maps to the judging criteria

- **Technological implementation:** a PayPal REST integration (OAuth2 client credentials, captures and
  refunds, Payouts, Invoicing create + send, Orders v2 checkout for agent purchases; all run against the real sandbox) with idempotency keys. Claude does real tool
  use and has read-only access to PayPal data. Tests cover the safety properties.
- **Design:** one screen. Inbox on the left, a summary strip (waiting / blocked / sent), then cards with the
  amount, the counterparty, the agent's reasoning, a plain-English reason when blocked, and the checks.
  One button to approve or reject. The audit trail sits below.
- **Potential impact:** small merchants and freelancers want AI to handle the back office, but they cannot
  risk a hallucinated or socially engineered payment. Countersign makes "AI does the work, I sign" a safe
  default for refunds, contractor payouts and invoicing.
- **Innovation:** safety is in the architecture, not the prompt. The model has no execute tool, its claims are
  checked against PayPal, approvals are bound to a digest of exactly what was shown, rules are re-checked at
  execution time, and the log is tamper-evident.
- **Agentic commerce:** the agent also *buys*: a stock alert becomes a supplier purchase proposal, priced
  from a human-vetted catalogue, paid through a PayPal checkout order that the owner signs twice.
- **Presentation:** the demo video on the Devpost page runs Claude (Opus 5.5) live against the PayPal sandbox: Claude
  refuses the CEO-fraud mail and the over-claimed refund itself, and code checks every card it does propose.

## Limits (honest)

- **The real Claude API has not been called yet** (no key); ClaudePlanner is tested with a fake client. The
  PayPal sandbox paths have run for real (see above), except paying the purchase checkout, which needs a
  human buyer login.
- Sandbox only, by design. The refunded total for sandbox captures comes from Countersign's own ledger, not
  from summing PayPal refunds.
- Approver accounts are local password logins on a 127.0.0.1 page (no TLS, no second factor, no roles or
  approval limits per person, sessions held in memory so a restart signs everyone out). A real deployment
  would put this behind the shop's identity provider (SSO/OIDC + MFA) and HTTPS; the session user would
  then be the IdP identity, and the auth method recorded would say so.
- The audit HMACs only stop someone who can write the log but cannot read `data/audit.key`. Anyone with the
  key file (for example root on the same machine) can rewrite and re-sign the log. A real deployment keeps the
  key in a secrets store / HSM or anchors the chain head externally.
- `verify-audit` alone cannot tell that the newest entries were deleted (the chain up to that point is still
  valid); anchoring the chain head outside the machine covers that too.
- Login throttling is in memory: per account (5), per client address (20), and one password check at a time.
  A flood of bad logins also slows down honest sign-ins. Fine for a 127.0.0.1 demo, not for the open internet.
- The inbox is a JSON file (`serve --inbox`); there is no live mail or helpdesk connector yet.
- Policy values live in `policy.py`. Changing them is a code change, which is the point.

## Tools used

- PayPal REST APIs (sandbox): OAuth2, Payments v2, Payouts v1, Invoicing v2, Orders v2 (checkout for
  agent-proposed purchases).
- Anthropic Claude (Messages API, tool use) as the optional planner, via the `anthropic` Python SDK.
- Claude was also used as a coding assistant while building this project.

## Licence

MIT. See `LICENSE`.
