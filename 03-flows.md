# Vera — Map / Flow Document

**Doc status:** v0.1 DRAFT — flows reflect the team's locked architecture. Details marked `⚠ VERIFY-ON-ZIP` (exact message/state names, conversation phases from `engagement-design.md`) will be tuned when the challenge materials arrive. Diagrams are Mermaid (renders in GitHub / VS Code / most viewers).

---

## 1. System Context (who talks to Vera)

```mermaid
graph LR
    J["Judge harness<br/>(automated)"] -- "POST /v1/context<br/>POST /v1/tick<br/>POST /v1/reply" --> V["Vera<br/>(FastAPI, in-memory)"]
    J -- "GET /v1/healthz<br/>GET /v1/metadata" --> V
    V -- "at most 1 structured call<br/>per outbound message" --> G["Gemini Flash API<br/>(structured JSON output)"]
    V -. "on quota/error:<br/>grounded fallback template" .-> V
```

- The judge is the **only** client that matters; it is also the clock (`/v1/tick` is the heartbeat — Vera never self-schedules).
- Gemini is the only external dependency, and only on the `send` path.

## 2. Data Flow — context push → composed message

```mermaid
flowchart TD
    A["Judge pushes context<br/>(category / merchant / customer / trigger)"] --> B{"POST /v1/context<br/>idempotency check<br/>(scope, context_id, version)"}
    B -- "already stored (same version)" --> B1["ACK, no-op"]
    B -- "new / newer version" --> C["Normalize via schemas.py<br/>(ignore unknown fields)"]
    C --> D["Upsert into ContextStore<br/>(category=1, merchants, customers, triggers)"]
    D --> E["Judge calls POST /v1/tick"]
    E --> F["Layer 1 — decision_engine.py<br/>(no LLM): expiry, suppression,<br/>dedup, urgency ranking, convo state"]
    F -- "nothing clears the bar" --> G1["actions: []  (silence is first-class)"]
    F -- "cache hit (merchant_id, trigger_id)" --> G2["Reuse cached composition"]
    F -- "best candidate, not cached" --> H["Layer 2 — composer.py:<br/>ONE Gemini Flash call<br/>(structured JSON)"]
    H -- "ok" --> I["Parse + validate {body, cta,<br/>suppression_key, rationale}"]
    H -- "quota/error/timeout" --> I2["Grounded fallback template<br/>(pure code, same context fields)"]
    I --> J["Write CompositionCache<br/>+ suppression state"]
    I2 --> J
    J --> K["Return actions[] to judge"]
```

**Key invariants:**
- Context payloads never trigger sends by themselves — only `/v1/tick` (and `/v1/reply` on the conversational path) emit actions.
- The **only** consumers of Gemini are ticks/replies where Layer 1 has already decided `send`.
- `suppression_key` from a composition is registered immediately so no later tick can re-send the same thing.

## 3. Request Lifecycle — `/v1/tick` (the main graded path)

```mermaid
sequenceDiagram
    participant J as Judge
    participant B as bot.py
    participant DE as decision_engine (L1)
    participant CP as composer (L2)
    participant G as Gemini Flash

    J->>B: POST /v1/tick
    B->>DE: evaluate(context_store, convo_store, cache)
    DE->>DE: 1) expire/prune dead triggers
    DE->>DE: 2) drop suppressed + already-sent keys
    DE->>DE: 3) rank remaining by urgency × fit × recency
    DE->>DE: 4) check conversation state (auto-reply? hostile? committed?)
    alt no candidate above threshold
        DE-->>B: decision = silence
        B-->>J: 200 {actions: []}
    else cache hit for (merchant_id, trigger_id)
        DE-->>B: decision = send (cached)
        B-->>J: 200 {actions: [cached message]}
    else fresh candidate
        DE-->>CP: compose(category, merchant, trigger, customer?)
        CP->>G: 1 structured call (JSON schema)
        G-->>CP: {body, cta, suppression_key, rationale}
        CP-->>B: composition (or fallback if error)
        B->>B: register suppression_key + cache
        B-->>J: 200 {actions: [message]}
    end
```

**Timing budget:** L1 ≈ milliseconds; L2 ≤ 12 s hard per attempt, whole request ≤ ~20 s worst case, far inside the 30 s ceiling.

## 4. Request Lifecycle — `/v1/reply` (bonus, conversational path)

```mermaid
sequenceDiagram
    participant J as Judge
    participant B as bot.py
    participant CH as conversation_handlers
    participant CP as composer (L2)

    J->>B: POST /v1/reply (merchant/customer message)
    B->>CH: respond(state, message)
    CH->>CH: classify: auto-reply? hostile/off-topic?<br/>intent commitment? normal interest?
    alt auto-reply (repeat pattern)
        CH-->>B: {response: "wait", reason}
    else hostile / off-topic
        CH-->>B: {response: "end", reason}
    else explicit commitment ("let's do it")
        CH-->>B: {response: "end", reason: intent logged, no pushy re-pitch}
    else genuine interest / question
        CH-->>B: {response: "send", intent_delta}
        B->>CP: compose follow-up (convo-aware constraints:<br/>no bot re-intro, single CTA, reference reply)
        CP-->>B: {body, cta, ...}
        B-->>J: 200 {response: "send", message}
    end
```

`⚠ VERIFY-ON-ZIP: exact `/v1/reply` response envelope + whether "send" must embed the message inline (api-call-examples.md).`

## 5. Conversation State Machine (conversation_handlers.py)

```mermaid
stateDiagram-v2
    [*] --> IDLE: first proactive message sent
    IDLE --> AWAITING: message delivered
    AWAITING --> AWAITING: auto-reply repeat detected → "wait"
    AWAITING --> ENGAGED: human shows interest / question
    AWAITING --> ENDED: hostile or off-topic → "end"
    AWAITING --> ENDED: explicit commitment ("let's do it") → log intent, "end"
    ENGAGED --> ENGAGED: follow-up composed (L2, convo-aware)
    ENGAGED --> ENDED: commitment / decline / silence past window
    ENDED --> [*]: suppression_key registered (no re-intro of bot)
```

| Transition | Detector (pure code) | Response |
|---|---|---|
| Auto-reply repeat | Same/similar short ack repeated N times (e.g., "ok", "busy now") | `wait` |
| Hostility / off-topic | Category/taboo-agnostic keyword + sentiment heuristics | `end` |
| Intent commitment | Affirmative-commitment phrases ("let's do it", "book it", "send details") | `end` (or logistics-only send if spec wants confirmation `⚠ VERIFY-ON-ZIP: engagement-design.md`) |
| Genuine interest | Everything else with a question/positive signal | `send` via L2 |

`⚠ VERIFY-ON-ZIP: state names, thresholds, and any officially suggested transitions come from engagement-design.md / engagement-research.md — tune this diagram then.`

## 6. Failure & Degradation Flow (quota protection in motion)

```mermaid
flowchart TD
    S["Send decision (L1)"] --> T{"Cache has<br/>(merchant_id, trigger_id)?"}
    T -- yes --> R["Return cached body/cta"]
    T -- no --> L["Gemini call attempt 1"]
    L -- 200 --> P["Parse + validate JSON"]
    L -- 429/quota --> K{"Another live key?"}
    K -- yes --> L2["Retry on next key"]
    K -- no --> FB
    L -- 5xx/timeout --> B2{"Elapsed < 20 s?"}
    B2 -- yes --> L
    B2 -- no --> FB["FALLBACK: grounded template<br/>(pure code, zero fabricated data,<br/>1 CTA, no URLs)"]
    L2 -- 200 --> P
    L2 -- fail --> FB
    P -- valid --> W["Cache write + suppress"]
    P -- invalid/unparseable --> FB
    FB --> W2["Cache write marked source=fallback"]
```

- Breaker: 3 consecutive LLM failures ⇒ 60 s all-fallback mode (visible in `/v1/healthz`).
- Fallback compositions are cached too — replays never re-attempt the LLM for the same pair.

## 7. Test-Day Timeline (how the harness consumes the above)

```mermaid
flowchart LR
    W["Warmup<br/>healthz/metadata + seed contexts"] --> T["60-min simulated window<br/>context pushes + ticks + replies"]
    T --> A["Adaptive injection<br/>novel contexts/triggers<br/>(must generalize)"]
    A --> R["Replay stress<br/>(idempotency + cache proof)"]
    R --> S["Scoring<br/>5 dimensions + penalties"]
```
