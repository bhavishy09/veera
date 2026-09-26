# Vera — Implementation Plan

**Doc status:** v0.1 DRAFT. Ordered build steps with exit criteria. **Step 0 is a hard gate**: no application code until the challenge zip is on disk and read. Every step's "done" line is checkable.

---

## Step 0 — Materials intake (HARD GATE — blocked on re-upload)

**Do:** unzip to `/home/z/my-project/challenge-materials/`; read in this order: `challenge-brief.md` → `challenge-testing-brief.md` (esp. §7 skeleton) → `api-call-examples.md` → `engagement-design.md` → `engagement-research.md` → `case-studies.md` → `judge_simulator.py` (source, not just usage) → `dataset/` + `generate_dataset.py`.
**Produce:** a one-page **field-name map** (actual wire field names for all 4 contexts + all 5 endpoint envelopes + judge CLI flags) appended to `04-backend-schema.md`; strike every `⚠ VERIFY-ON-ZIP` marker or convert it to confirmed.
**Exit:** all ⚠ markers resolved; docs 1–4 updated to v1.0; user has confirmed the planning docs.

## Step 1 — Skeleton endpoints (bot.py + schemas.py + config.py)

**Do:** FastAPI app with all 6 routes (5 required + teardown) returning schema-conformant stubs; global JSON error handler; pydantic models with `extra="ignore"`; env config; `uvicorn bot:app --host 0.0.0.0 --port $PORT` verified; healthz reports real (empty) counts.
**Exit:** curl smoke test on every endpoint returns well-formed JSON in < 100 ms; malformed bodies → clean 4xx JSON, no 500s.

## Step 2 — Store layer (store.py)

**Do:** ContextStore / SuppressionStore / CompositionCache / ConversationStore / DecisionLog exactly per `04-backend-schema.md`; idempotency by `(scope, context_id, version)`; teardown wipe.
**Exit:** unit tests: duplicate push no-op, newer version replaces, older version ignored, teardown clears everything.

## Step 3 — Layer 1 decision engine (decision_engine.py)

**Do:** the 6-step decision table (expiry → suppression → dedup → conversation gate → ranked selection with silence-wins → per-tick budget). Pure functions; no LLM imports; category behavior sourced only from context payloads.
**Exit:** decision-table unit tests pass, including: expired trigger never composes; suppressed key never returns; silence wins on a store of only low-value triggers; top-ranked candidate returned on mixed store.

## Step 4 — Layer 2 composer (composer.py)

**Do (in order):**
1. Fallback template builder first (grounded, 1 CTA, no URLs, no invented numbers) — this guarantees the bot is fully functional with `DISABLE_LLM=1` before any Gemini code exists.
2. Prompt builder: persona from `CategoryContext.voice`, rubric-aware system prompt (5 dimensions), constraint list (penalty table), embedded silent mini-reasoning, strict JSON schema, one light original example (self-written, cross-checked against case studies only AFTER the zip arrives to ensure non-similarity).
3. Gemini REST client via httpx: timeout 12 s, one retry, key rotation on 429, circuit breaker (3 fails ⇒ 60 s fallback mode), cache write on every composition (llm or fallback).
**Exit:** with a valid key — 3 seeded compositions return schema-valid JSON grounded in the given contexts (manual fact audit); with an invalid key — same inputs produce fallback messages, all well-formed, < 2 s; unit test: cache hit ⇒ zero HTTP calls.

## Step 5 — Conversation state machine (conversation_handlers.py)

**Do:** `respond(state, merchant_message) -> dict` implementing auto-reply detection (repeat short-ack pattern), hostility/off-topic handling, intent-transition handling (commitment ⇒ end + intent log), genuine-interest ⇒ send; state transitions per the state diagram in `03-flows.md`; wires into `/v1/reply` and Layer 1's conversation gate.
**Exit:** table-driven unit tests covering every transition edge in the diagram; no LLM call reachable from any wait/end path.

## Step 6 — Local validation loop (judge_simulator.py)

**Do:** run the simulator against a local uvicorn instance in three modes: `DISABLE_LLM=1` (structural), valid key (quality), invalid key (degradation drill). Instrument DecisionLog to explain every send and every silence.
**Exit:** `judge_simulator.py` completes cleanly with **non-zero scores on all 5 dimensions**; zero duplicate sends across replay stress; no response anywhere near 30 s; iterate thresholds (silence bar, urgency weights, auto-reply N) until scores stop improving.

## Step 7 — Generalization proof (data-drivenness)

**Do:** author two toy CategoryContext JSONs from a different vertical (e.g., salon) using ONLY the CategoryContext schema; run ticks through the full pipeline; verify voice/lever/taboos change with data alone.
**Exit:** ≥ 5 novel-trigger scenarios × 2 categories compose correctly with zero code edits — grep-audit confirms no category literals in source.

## Step 8 — submission.jsonl (scripts/gen_submission.py)

**Do:** replicate the canonical 30 test pairs exactly as `generate_dataset.py` defines them (`⚠ VERIFY-ON-ZIP: derivation`); run compositions (LLM if key available, else fallback per user's choice); emit exactly 30 lines with `test_id, body, cta, send_as, suppression_key, rationale`.
**Exit:** 30/30 lines parse; field-set matches spec; plagiarism self-check vs `case-studies.md` clean; determinism check (re-run ⇒ identical file, given same mode).

## Step 9 — README + submission packaging

**Do:** finalize the drafted README (approach, architecture, stack, quota protection, scope, files; team name/members/contact as placeholders for the user); final checklist against the PRD's Definition of Done; confirm file layout matches testing-brief §7.
**Exit:** Definition-of-Done checklist fully ticked; user reviews final package.

## Risk Register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Wire schemas differ from provisional guesses | High (until zip lands) | All parsing isolated in `schemas.py`; normalization layer; Step 0 field-name map |
| Gemini free-tier RPM exhausted mid-test | Medium | L1 gating + cache + rotation + breaker + fallback (all designed in) |
| Silence-vs-send threshold mis-tuned (Decision Quality loss) | Medium | DecisionLog + judge_simulator iteration loop in Step 6 |
| Near-duplicate of case studies slipping into prompt example | Low | Example written from scratch; Step 8 similarity self-check |
| Multi-worker state forks on deploy | Low | Single worker enforced in start command + README note |
| Response envelope mismatch discovered only at judging | Medium | Step 0 map + Step 6 simulator run against real shapes |

## Est. effort (focused hours)

| Step | Est. |
|---|---|
| 0 Intake + doc finalize | 1–2 h |
| 1 Skeleton | 1–2 h |
| 2 Store | 1–2 h |
| 3 Layer 1 | 2–3 h |
| 4 Layer 2 | 3–4 h |
| 5 Conversation | 2 h |
| 6 Simulator loop | 2–4 h (iteration-heavy) |
| 7 Generalization proof | 1 h |
| 8 submission.jsonl | 1 h |
| 9 README/package | 0.5 h |
