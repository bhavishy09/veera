# Vera — Tech Stack & API/Tools Document

**Doc status:** v0.1 DRAFT — `⚠ VERIFY-ON-ZIP` marks every slot that must be confirmed against `challenge-testing-brief.md` (esp. §7 reference skeleton), `api-call-examples.md`, and the Gemini API docs before implementation.

---

## 1. Runtime & Language

| Choice | Version | Rationale |
|---|---|---|
| Python | 3.11+ | Team fluency; FastAPI ecosystem; `uvicorn bot:app` matches the required entrypoint convention |
| Process model | Single uvicorn worker | In-memory state is the source of truth — multiple workers would fork state. Single worker + asyncio is ample for one judge harness |

## 2. Libraries (pinned in `requirements.txt`)

| Library | Purpose | Notes |
|---|---|---|
| `fastapi` | HTTP server, routing, validation | Required by spec |
| `uvicorn[standard]` | ASGI server | `uvicorn bot:app --host 0.0.0.0 --port $PORT` |
| `pydantic` v2 | Request/response models + forward-compatible parsing (`extra="ignore"`) | Single normalization point for wire shapes |
| `httpx` | Gemini REST calls with hard timeouts | Chosen over `google-generativeai` SDK: fewer dependency risks on deploy targets, full control over timeouts/retries/rotation, and the REST `responseSchema` structured-output path is stable and well-documented. *Trade-off noted; revisit only if the SDK proves simpler for structured output* |
| `python-dotenv` | Local env loading | Prod config purely env vars |

Deliberately **not** included: any DB driver (in-memory only per spec), any agent framework (single-shot prompt design), any frontend stack (not graded).

## 3. Gemini API Integration

### 3.1 Model selection
- **Primary:** `gemini-2.5-flash` (configurable via `GEMINI_MODEL` env; drop to an alternate Flash variant via env if free-tier RPM/TPM limits bite on test day).
- **Never** a Pro model. Flash has the higher free-tier ceiling and sufficient quality for short WhatsApp-format composition.

### 3.2 Call shape (one call per outbound message — hard cap)
- Endpoint: `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` with `x-goog-api-key` header.
- **Structured output:** `generationConfig.response_mime_type = "application/json"` + `response_schema` for:

```json
{
  "body": "string — the WhatsApp message text",
  "cta": "string — exactly one call to action",
  "suppression_key": "string — stable dedup key for this composition",
  "rationale": "string — why now, tied to the trigger"
}
```

- Sampling: moderate temperature (≈0.6) for natural phrasing, low enough for grounded consistency. Max output tokens capped small (message is ≤ a few hundred words).
- System prompt: persona (from `CategoryContext.voice`) + rubric-aware 5-dimension framing + anti-pattern constraints + embedded silent mini-reasoning ("first silently pick the single best compulsion lever for this trigger, then write using it — return ONLY the final JSON, not your reasoning") + one light, original, shape-only example.
- User prompt payload: the selected CategoryContext + MerchantContext + TriggerContext (+ CustomerContext if present) as **clean, pruned JSON** — irrelevant fields stripped to reduce noise and hallucination surface.

### 3.3 Timeout / retry / degradation ladder (protecting the 30 s ceiling)
```
attempt 1: key[0], timeout 12 s
  ├─ 200 → parse → validate against schema → return
  ├─ 429 / RESOURCE_EXHAUSTED → rotate to next live key → retry once (timeout 12 s)
  ├─ 5xx / network → backoff 2 s → retry once on same key
  └─ all attempts fail OR total elapsed > 20 s → FALLBACK TEMPLATE (still grounded, still JSON, < 1 s)
```
- Fallback template builder is **pure code**: derives body/cta from the same context fields (e.g., offer name from CategoryContext offer catalog, merchant name, trigger reason) — zero fabricated numbers, one CTA, no URLs.
- **Circuit breaker:** after 3 consecutive LLM failures, trip the breaker for 60 s — all sends use fallback until it resets. Status visible on `/v1/healthz`.

### 3.4 Key rotation
- `GEMINI_API_KEYS="key1,key2,key3"` (comma-separated). A single `GEMINI_API_KEY` is also accepted.
- Round-robin start position; keys marked exhausted on 429 for the process lifetime; breaker shares state across keys.

### 3.5 Quota accounting (why we'll stay free-tier safe)
| Lever | Effect |
|---|---|
| Decide-before-call (Layer 1) | No LLM spend on wait/end/silence decisions |
| `(merchant_id, trigger_id)` cache | Replay stress tests cost **zero** LLM calls |
| One-shot prompt (no agent chain) | Exactly 1 call per unique composition |
| Flash + rotation + breaker | Survives bursts and mid-test exhaustion |

Worst-case sizing: 60-min window with ~100 triggers seen (`⚠ VERIFY-ON-ZIP: dataset trigger count`), Layer 1 expected to gate ~60–70% → ~30 unique LLM calls ≈ well inside Flash free tier even at conservative RPM, with replays free.

## 4. Endpoint → Code Module Map

**Final file layout must conform to `challenge-testing-brief.md` §7 reference skeleton.** `⚠ VERIFY-ON-ZIP` Planned shape (built out from the skeleton, not replacing it):

| Endpoint | Handler location | Delegates to |
|---|---|---|
| `POST /v1/context` | `bot.py` route | `store.py` (idempotency + upsert), `schemas.py` (normalize) |
| `POST /v1/tick` | `bot.py` route | `decision_engine.py` (Layer 1) → `composer.py` (Layer 2) → `store.py` (cache/suppress) |
| `POST /v1/reply` | `bot.py` route | `conversation_handlers.respond(state, merchant_message)` → optionally `composer.py` |
| `GET /v1/healthz` | `bot.py` route | `store.py` counts, `composer.py` breaker status |
| `GET /v1/metadata` | `bot.py` route | `config.py` (team/model identity) |
| `POST /v1/teardown` (optional) | `bot.py` route | `store.py` reset |

### Module responsibilities
| Module | Owns | Forbidden |
|---|---|---|
| `bot.py` | FastAPI app, routes, response envelopes, error handlers that guarantee well-formed JSON on ANY input | Business logic |
| `store.py` | All in-memory state (contexts, suppression, cache, conversations), idempotency by `(scope, context_id, version)`, thread-async safety | Any LLM or category logic |
| `decision_engine.py` | Layer 1: expiry, urgency ranking, dedup, suppression, silence-wins, candidate selection | LLM calls, string composition |
| `composer.py` | Layer 2: prompt builder, Gemini client, rotation, breaker, fallback template, cache write | Any decision-making |
| `conversation_handlers.py` | Multi-turn state machine: `respond(state, merchant_message) -> dict`; auto-reply, intent-transition, hostility/off-topic | LLM calls (returns decisions; caller invokes composer) |
| `schemas.py` | Pydantic wire models + normalization of the 4 contexts | State |
| `config.py` | Env config, team metadata placeholders | — |
| `scripts/gen_submission.py` | Builds `submission.jsonl` (30 lines) from dataset + running compositions offline | — |

## 5. Configuration (env vars)

| Var | Default | Meaning |
|---|---|---|
| `PORT` | `8000` | Bind port (never hardcoded) |
| `GEMINI_API_KEYS` | *(empty)* | Comma-separated key list; empty ⇒ offline/fallback mode |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Composer model |
| `COMPOSER_TIMEOUT_S` | `12` | Per-attempt LLM timeout |
| `DISABLE_LLM` | `0` | `1` ⇒ always fallback templates (quota-free structural testing) |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `TEAM_NAME` / `TEAM_MEMBERS` / `CONTACT_EMAIL` | placeholders | Served by `/v1/metadata` — user fills before submission |

## 6. Local Validation Tooling

- **`judge_simulator.py`** (provided): the gate. Run against a local uvicorn instance; must finish with non-zero scores on all 5 dimensions. `⚠ VERIFY-ON-ZIP: exact CLI invocation and any flags`
- **Test mode matrix:**
  1. `DISABLE_LLM=1` — structural/conformance pass (no quota used)
  2. Valid key — full-quality pass (a handful of real compositions)
  3. Invalid key — degradation drill (all sends still well-formed via fallback)
- **Unit tests** (`pytest`, not shipped in submission): Layer 1 decision table, idempotency, cache hit path, conversation state machine transitions, fallback template grounding (asserts no numbers appear that aren't in the given context).

## 7. Deployment Posture (final target parked)

- Anything that speaks HTTP + env vars works: start command `uvicorn bot:app --host 0.0.0.0 --port $PORT`.
- No filesystem writes, no DB, no websockets, no scheduler inside the app (the judge's `/v1/tick` **is** the scheduler).
- Railway leading candidate when decided (no cold-start sleep); Vercel optionally for a cosmetic frontend only. Nothing in code may assume either.
