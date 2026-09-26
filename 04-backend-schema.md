# Vera — Backend Schema Document

**Doc status:** v0.1 DRAFT. Internal store schemas are design-complete (they are our code). **Wire schemas are provisional** — every request/response field that must match `api-call-examples.md` exactly is marked `⚠ VERIFY-ON-ZIP`. All wire parsing lives in `schemas.py` so real shapes are a one-file adaptation.

---

## 1. In-Memory Data Model (store.py, single process, asyncio-safe)

### 1.1 ContextStore
```python
contexts = {
    "category":  VersionedContext | None,          # single active category
    "merchants": {merchant_id: VersionedContext},  # keyed by merchant context_id
    "customers": {customer_id: VersionedContext},  # keyed by customer context_id
    "triggers":  {trigger_id:  TriggerRecord},
}
```
```python
@dataclass
class VersionedContext:
    scope: str            # "category" | "merchant" | "customer"
    context_id: str
    version: int
    payload: dict         # normalized raw JSON (unknown fields preserved)
    received_at: float    # server clock, monotonic-ish ordering

@dataclass
class TriggerRecord:
    ctx: VersionedContext
    merchant_id: str | None      # linked if derivable from payload ⚠ VERIFY-ON-ZIP
    customer_id: str | None      # linked if CustomerContext present
    expires_at: float | None     # derived from payload fields ⚠ VERIFY-ON-ZIP
    status: "open" | "acted" | "expired" | "suppressed"
```

**Idempotency rule (FR-1):** on `/v1/context`, compute key `(scope, context_id, version)`:
- key already stored with `version <= stored.version` → ACK, no mutation
- `version > stored.version` → replace payload, update `received_at`
- storage key itself remains `context_id` (latest version wins)

### 1.2 SuppressionStore
```python
suppressions = {suppression_key: SuppressionRecord}
@dataclass
class SuppressionRecord:
    reason: str          # "composed" | "hostile" | "ended" | "auto_reply" | "manual"
    created_at: float
    merchant_id: str | None
    trigger_id: str | None
    # Phase 1: no TTL decay — suppression lasts process lifetime.
    # ⚠ VERIFY-ON-ZIP: does any spec section require re-eligibility after N hours?
```

### 1.3 CompositionCache (quota protection core)
```python
composition_cache = {(merchant_id, trigger_id): CachedComposition}
@dataclass
class CachedComposition:
    body: str
    cta: str
    suppression_key: str
    rationale: str
    source: "llm" | "fallback"
    composed_at: float
```
- Hit ⇒ deterministic reuse, **zero** LLM calls on replay stress tests.
- Also keyed defensively by `suppression_key` index for reverse lookups.

### 1.4 ConversationStore (conversation_handlers.py)
```python
conversations = {conversation_key: ConversationState}
# conversation_key: merchant_id, or f"{merchant_id}:{customer_id}" when customer-scoped ⚠ VERIFY-ON-ZIP

@dataclass
class ConversationState:
    phase: "idle" | "awaiting" | "engaged" | "ended"
    auto_reply_count: int          # consecutive machine-like acks
    hostility_signals: int
    pending_intent: str | None     # e.g. "booking", "pricing"
    last_outbound: dict | None     # what we last sent (body, trigger_id, at)
    history: list[dict]            # [{dir: in|out, text, at}] capped length
    ended_reason: str | None
```

### 1.5 DecisionLog (debugging + scoring self-audit; ring buffer)
```python
decision_log: deque[maxlen=500]  # every L1 decision: ts, tick_id, candidate, verdict, reason
```
Not required by spec; invaluable for tuning silence-vs-send thresholds during local `judge_simulator.py` runs.

## 2. Endpoint Schemas (wire)

> **Notation:** `⚠V` = field name/shape provisional pending `api-call-examples.md`; unmarked = fixed by the team's spec or trivial (health/metadata).

### 2.1 `POST /v1/context`
Request (one push = one context object):
```jsonc
{
  "scope": "category | merchant | customer | trigger",   // ⚠V exact enum values
  "context_id": "string",                                 // ⚠V (may be implied per type)
  "version": 1,                                           // ⚠V
  "data": { ...CategoryContext | MerchantContext |
            TriggerContext | CustomerContext... }         // ⚠V exact inner fields (dataset/*.json)
}
```
Response (ack):
```jsonc
{ "status": "accepted" }        // ⚠V exact ack shape; may include stored/ignored flag
```
Notes: unknown `scope` values → 200-level ack + ignore (never 5xx); malformed → 4xx with JSON error body, state untouched.

### 2.2 `POST /v1/tick`
Request:
```jsonc
{ "now": "2026-01-01T10:00:00Z" }   // ⚠V does the harness pass simulated time? If absent → server clock
```
Response:
```jsonc
{
  "actions": [                       // zero or more
    {
      "type": "send_message",        // ⚠V exact action type string
      "send_as": "merchant | platform",  // ⚠V who speaks (also a submission.jsonl field)
      "to": "merchant_id | customer_id", // ⚠V addressing convention
      "body": "composed message text",
      "cta": "exactly one call to action",
      "suppression_key": "stable dedup key",
      "rationale": "why now, tied to trigger"
    }
  ]
}
```
Guarantees: ≤ 30 s total; never empty-body/malformed; empty `actions` array is valid and frequent.

### 2.3 `POST /v1/reply`
Request:
```jsonc
{
  "merchant_id": "string",           // ⚠V
  "customer_id": "string | missing", // ⚠V
  "message": { "text": "...", "at": "..."}   // ⚠V shape of inbound message
}
```
Response — decision must be exactly one of `send | wait | end`:
```jsonc
{ "response": "send",              // ⚠V field name for the decision enum
  "message": { "body": "...", "cta": "..." },   // present iff "send"  ⚠V
  "reason": "auto_reply | hostile | intent_committed | engaged | ..." }
```

### 2.4 `GET /v1/healthz`
```jsonc
{ "status": "ok", "uptime_s": 123.4,
  "contexts": { "category": 1, "merchant": 7, "customer": 3, "trigger": 21 },
  "llm": { "mode": "gemini-flash | fallback | disabled",
           "circuit": "closed | open", "keys_live": 2 } }
```
(`llm` block is our own addition — harmless extra fields unless spec forbids them `⚠V`.)

### 2.5 `GET /v1/metadata`
```jsonc
{ "team_name": "«YOUR TEAM NAME»",          // placeholder — user fills
  "team_members": "«MEMBERS»",
  "contact_email": "«EMAIL»",
  "bot_name": "Vera",
  "model": "gemini-2.5-flash",
  "approach": "2-layer: rule-based decision + single-shot Gemini Flash composer" }
```
`⚠V exact expected keys per testing brief (e.g. `team_id`?).`

### 2.6 `POST /v1/teardown` (optional, local convenience)
```jsonc
// request: {}  response: { "status": "torn_down", "cleared": {..counts..} }
```

## 3. Error Envelope (all endpoints)
```jsonc
{ "error": { "code": "bad_request | not_found | internal", "message": "human-readable" } }
```
- Global exception handler in `bot.py`: **any** unhandled exception → 200/4xx JSON if spec allows, else 500 JSON — but the L1/L2 design makes empty/malformed responses structurally impossible on action paths (fallback template guarantees a valid action).
- Request body size cap + JSON parse guard → 400, never a crash.

## 4. Layer 1 Decision Table (decision_engine.py — the code-half of Decision Quality)

| # | Check (in order) | Data source | Outcome if triggered |
|---|---|---|---|
| 1 | Trigger expired? | TriggerRecord.expires_at | status=expired, skip |
| 2 | Suppression key present? | SuppressionStore | skip |
| 3 | `(merchant_id, trigger_id)` already composed? | CompositionCache | serve cached (if still eligible) else skip |
| 4 | Conversation gate: auto-reply repeat / hostile / committed? | ConversationStore | wait / end / suppress |
| 5 | Urgency × fit × recency ranking | context payloads (data-driven weights) | pick top candidate or **silence** if below bar |
| 6 | Budget guard: max sends per tick / per merchant per window | config | defer to next tick |

- All thresholds live in `config.py` as plain constants — **no category names anywhere**; per-category behavior comes only from CategoryContext payload fields.
- Step 5's "silence below bar" is the anti-spam half of Decision Quality; tuned against judge_simulator until scores peak.

## 5. Composer Output Contract (Layer 2)
```jsonc
{ "body": "string, WhatsApp-length, category voice, grounded facts only",
  "cta": "exactly one",
  "suppression_key": "e.g. merchant:{mid}:trigger:{tid}:{type}",   // deterministic template
  "rationale": "1–2 sentences: why now, referencing the trigger explicitly" }
```
Validation before returning: non-empty body/cta; exactly one CTA marker; no URL pattern in body; no digits present that aren't traceable to provided context (regex cross-check against payload numbers). Violations → fallback template, not a retry loop.
