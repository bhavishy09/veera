# Vera — Product Requirements Document (PRD)

**Project:** magicpin AI Challenge — "Vera" proactive merchant-messaging bot
**Phase:** 1 (dentistry vertical, submission build)
**Doc status:** v0.1 DRAFT — built from the team's decision brief. Items marked `⚠ VERIFY-ON-ZIP` must be confirmed against the challenge materials (`challenge-brief.md`, `challenge-testing-brief.md`, `engagement-design.md`, `engagement-research.md`, `api-call-examples.md`, `case-studies.md`, `judge_simulator.py`, `dataset/`) before this PRD is final. **The zip did not reach the agent filesystem on first attempt — re-upload required.**

---

## 1. Problem Statement

magicpin wants a WhatsApp-style assistant for its merchants. Today, merchants hear from the platform only when *they* reach out. Vera flips that: it **decides when to proactively message a merchant** (or, on the merchant's behalf, one of their customers) **and writes that message**, grounded in four inputs:

1. **CategoryContext** — vertical-level rules and knowledge (for Phase 1: dentistry)
2. **MerchantContext** — the specific business's profile, history, and stats
3. **TriggerContext** — the *why now* signal (event, expiry, milestone, window)
4. **CustomerContext** *(optional)* — a specific end-customer the merchant may want to reach

Vera is graded by an **automated judge harness** that drives the bot over HTTP across a simulated test window and scores output on 5 dimensions (§5). The bot that wins is not the one that talks the most — it is the one that **chooses the right moment, explains why now, and writes a message a real merchant would act on** — while never wasting LLM budget or crashing under stress.

## 2. Scope

### 2.1 In scope (Phase 1)
- **Dentistry vertical only** — but with **zero category-specific hardcoding**. Every category rule (voice/tone, taboos, offer catalog, peer stats, digest cadence, seasonal beats) is read from the `CategoryContext` JSON at runtime. The architecture must accept a salon/gym/restaurant/pharmacy CategoryContext tomorrow with **no code changes**.
- All **5 required endpoints** (`/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, `/v1/metadata`) + optional `/v1/teardown`.
- The **bonus `conversation_handlers.py` module** (multi-turn state machine) — in scope, not optional.
- **`submission.jsonl`** — exactly 30 lines, one per canonical test pair from `generate_dataset.py` (`⚠ VERIFY-ON-ZIP: exact derivation + field order`).
- Local validation via the provided **`judge_simulator.py`**, passing with **non-zero scores**.

### 2.2 Out of scope (Phase 1)
- Category data packs for other verticals (salons, gyms, restaurants, pharmacies) — Phase 2 supplies these as JSON only.
- Finalized deployment target (Railway leading candidate; must remain deployment-agnostic: `uvicorn bot:app --host 0.0.0.0 --port $PORT`, env-var config, no hardcoded hosts).
- Any persistence beyond process lifetime (spec allows in-memory; no restarts occur mid-test).
- Any cosmetic frontend (not graded).

## 3. The 4-Context Framework

| Context | Required? | Carries (per brief) | Vera uses it for |
|---|---|---|---|
| **CategoryContext** | Yes (one active) | voice/tone, taboos, offer catalog, peer stats, digest config, seasonal beats | Layer 2 persona + constraint injection; Layer 1 category-aware validity windows and lever selection |
| **MerchantContext** | Yes | merchant profile, performance stats, history (`⚠ VERIFY-ON-ZIP: exact fields`) | Merchant Fit + Specificity grounding; personalization levers |
| **TriggerContext** | Yes (the "why now") | trigger type/id, timing/urgency, expiry (`⚠ VERIFY-ON-ZIP: exact fields`) | Layer 1 selection ranking, expiry filtering, why-now rationale |
| **CustomerContext** | Optional | individual customer profile/history (`⚠ VERIFY-ON-ZIP`) | `send_as` targeting (merchant vs. merchant→customer), message personalization |

**Wire field names are NOT yet confirmed** — all parsing must go through a single normalization layer (`schemas.py`) so that adjusting to the real payload shapes is a one-file change. `⚠ VERIFY-ON-ZIP: dataset/*.json + api-call-examples.md`

## 4. Functional Requirements

### FR-1 — Context ingestion: `POST /v1/context`
- Accept pushes of any of the four context types; **idempotent by `(scope, context_id, version)`** — replays of the same version are acknowledged without duplicating or overwriting newer state; higher versions replace lower.
- Must respond quickly and never error on well-formed-but-unknown fields (forward-compatible parsing: ignore unknown keys).

### FR-2 — Proactive engine: `POST /v1/tick`
- Inspect current context store; return **zero or more** proactive actions.
- Layer 1 (deterministic, no LLM) selects **at most one best trigger** to act on per tick unless multiple independent (merchant, trigger) pairs clearly merit separate actions (`⚠ VERIFY-ON-ZIP: whether multi-action ticks are expected`).
- Suppressed/expired/already-composed triggers are excluded or served from cache.
- **Decision Quality half:** choosing to return `actions: []` is a first-class outcome — silence must win whenever no signal clears the bar.

### FR-3 — Reply handler: `POST /v1/reply`
- Synchronous handling of an incoming merchant/customer reply.
- Returns a decision: **`send` / `wait` / `end`** (`⚠ VERIFY-ON-ZIP: exact response envelope from api-call-examples.md`).
- Routing through `conversation_handlers.respond(state, merchant_message) -> dict`: auto-reply detection, intent-transition handling, hostility/off-topic handling.
- Layer 2 LLM call only on `send`.

### FR-4 — Liveness: `GET /v1/healthz`
- 200 OK + loaded-context counts (category/merchant/customer/trigger tallies) + uptime + LLM circuit status.

### FR-5 — Identity: `GET /v1/metadata`
- Team name, model identity (Gemini Flash), version. Team fields left as placeholders until the user fills them.

### FR-6 (optional) — `POST /v1/teardown`
- Wipes all in-memory state; used between local test runs.

### FR-7 — Composer (Layer 2)
- One structured prompt per composition: persona from `CategoryContext.voice`, rubric-aware system prompt (§5), explicit anti-pattern constraints, embedded silent mini-reasoning, **strict JSON schema output** `{body, cta, suppression_key, rationale}`.
- One light, **original** example in the prompt — never copied or near-copied from `case-studies.md` (judge runs a similarity/plagiarism check `⚠ VERIFY-ON-ZIP: mechanics`).

### FR-8 — Quota protection (first-class requirement)
1. Cache & reuse by `(merchant_id, trigger_id)` — never re-call Gemini for the same pair.
2. Decide before calling — Layer 1 gates every LLM call; no LLM spend on "don't send."
3. Gemini **Flash**, never Pro.
4. Graceful degradation: on quota/rate-limit/error → grounded fallback template from the same context fields, within the response budget, never empty/malformed, never hanging past 30 s.
5. Optional 2–3 API key rotation via env config.

## 5. Scoring Rubric (judge's 5 dimensions)

| # | Dimension (live site) | Also known as (docs) | Owned by | How Vera optimizes |
|---|---|---|---|---|
| 1 | Specificity | — | Layer 2 | Ground every message in actual context numbers/facts only; no invented data |
| 2 | Category Fit | — | Layer 1 + 2 | Voice/tone/taboos strictly from CategoryContext; data-driven, not hardcoded |
| 3 | Merchant Fit | — | Layer 2 | Personalize to merchant stats/history from MerchantContext |
| 4 | **Decision Quality** | **"Trigger Relevance"** (merged — see note) | **Layer 1** + explicit why-now in the message | Code picks the single best signal (or silence); message states *why now* tied to the trigger |
| 5 | Engagement Compulsion | — | Layer 2 | Exactly one CTA, friction-minimal, lever chosen per trigger type |

> **Merged-dimension note:** challenge docs say "Trigger Relevance"; the live site says "Decision Quality." Treated as ONE dimension with two halves: (a) the code picks the right signal — including sending nothing; (b) the composed message explicitly and clearly explains why now, tied to the specific trigger. Prompt and Layer 1 are both designed against both halves. `⚠ VERIFY-ON-ZIP: confirm exact rubric weights if listed in challenge-brief.md`

## 6. Hard Penalties / Failure Modes to Avoid

1. **Invented facts or numbers** not present in the provided context.
2. **More than one CTA** in a message.
3. **URLs in the message body.**
4. **Generic discount framing** (taboo per category rules).
5. **Re-introducing the bot** mid-conversation.
6. **Empty, malformed, or overdue responses** — nothing may hang past **30 s**; LLM failure must degrade to the fallback template, never to a blank.
7. **Plagiarism/near-duplication** of `case-studies.md` bodies (similarity check).
8. **Broken idempotency** — double-composing or state corruption on context replays (replay stress tests exist precisely to catch this).
9. **Category hardcoding** — any `if category == "dentist"` in code is a Phase-2 design defect.

`⚠ VERIFY-ON-ZIP: cross-check this list against the official penalty table in challenge-brief.md / challenge-testing-brief.md`

## 7. Non-Functional Requirements

- **Latency:** every endpoint well under the 30 s ceiling; target p95 < 3 s on non-LLM paths, < 15 s worst-case LLM path (timeout 12 s + one bounded retry).
- **Reliability:** no crash paths on malformed/unknown input; LLM circuit breaker + key rotation; in-memory only, no restart support required.
- **Determinism where it matters:** Layer 1 is pure code; cache makes replay tests byte-stable.
- **Portability:** std `uvicorn` entrypoint, 12-factor env config, no localhost assumptions, requirements.txt pinned.
- **Testability:** `DISABLE_LLM=1` offline mode (fallback templates only) so `judge_simulator.py` can run structurally without burning quota.

## 8. Definition of Done

- [ ] All 5 endpoints (+ optional teardown) conform to `api-call-examples.md` shapes exactly.
- [ ] `judge_simulator.py` passes locally with **non-zero scores on all 5 dimensions**.
- [ ] Replay stress tests produce **zero duplicate sends** (cache + suppression proven).
- [ ] Adaptive-injection-style novel triggers (not in the 100 sample set) compose correctly — generalization spot-check with ≥5 hand-made contexts across at least 2 hypothetical category JSONs (proves data-drivenness).
- [ ] LLM failure drill: with a deliberately invalid key, all sends still return grounded, well-formed messages via fallback within budget.
- [ ] `submission.jsonl` — exactly 30 valid lines, fields `test_id, body, cta, send_as, suppression_key, rationale` (`⚠ VERIFY-ON-ZIP: exact field names/order`).
- [ ] `conversation_handlers.py` implements and unit-passes the 4 reply classes (auto-reply, intent-transition, hostility/off-topic, normal).
- [ ] `README.md` finalized with team info placeholders filled by user.
- [ ] No category-specific branching anywhere in code (grep-audit).
- [ ] Plagiarism self-check: no composed body is a near-duplicate of any case study.
