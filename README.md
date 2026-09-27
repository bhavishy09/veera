# Vera — magicpin AI Challenge

[![Live Deployment](https://img.shields.io/badge/Render-Live%20Backend-24c8db?style=for-the-badge&logo=render&logoColor=white)](https://veera-bot-pjqr.onrender.com)
[![Status](https://img.shields.io/badge/API-Operational-brightgreen?style=for-the-badge)](https://veera-bot-pjqr.onrender.com/v1/healthz)
[![Model](https://img.shields.io/badge/Model-Gemini%203.1%20Flash%20Lite-orange?style=for-the-badge&logo=google)](https://veera-bot-pjqr.onrender.com/v1/metadata)

> 🚀 **Live Production Backend URL:** [`https://veera-bot-pjqr.onrender.com`](https://veera-bot-pjqr.onrender.com)

Vera is an intelligent, autonomous WhatsApp messaging engine built for the **magicpin AI Challenge**. It empowers small local merchants across 5 key verticals (dentists, salons, restaurants, gyms, and pharmacies) to engage customers and drive operational growth through context-aware, hyper-personalized, and policy-compliant WhatsApp messages.

---

## 1. Architecture: Two-Layer Design

Vera is built with a decoupled, high-resilience architecture:

```
                  ┌─────────────────────────────────────────┐
                  │          Inbound Event / Context         │
                  │ (Category, Merchant, Customer, Trigger) │
                  └────────────────────┬────────────────────┘
                                       │
                                       ▼
                  ┌─────────────────────────────────────────┐
                  │       LAYER 1: DECISION ENGINE          │
                  │   - Business rules & urgency scoring     │
                  │   - Silence-bar filtering (noise control)│
                  │   - Deduplication & suppression tracking │
                  │   - Decides: Send vs. Suppress           │
                  └────────────────────┬────────────────────┘
                                       │
                         [ Decision = SEND MESSAGE ]
                                       │
                                       ▼
                  ┌─────────────────────────────────────────┐
                  │       LAYER 2: COMPOSITION ENGINE       │
                  │   - Exactly 1 Gemini Flash call         │
                  │   - Category persona & taboo checks      │
                  │   - Anti-fabrication numeric validation │
                  │   - Strict single-CTA enforcement        │
                  │   - Fail-fast grounded template fallback│
                  └────────────────────┬────────────────────┘
                                       │
                                       ▼
                  ┌─────────────────────────────────────────┐
                  │     Outbound WhatsApp Action Object     │
                  └─────────────────────────────────────────┘
```

1. **Layer 1: Deterministic Decision Engine**
   - Ingests merchant, customer, category, and trigger signals into in-memory stores.
   - Computes weighted urgency, business alignment, and silence-bar gates to ensure merchants are never spammed.
   - Evaluates active suppressions and caches before deciding whether an outreach is justified.

2. **Layer 2: Single-Shot Generative Composer**
   - If Layer 1 decides to send, Layer 2 executes **at most one** call to `gemini-3.1-flash-lite`.
   - Strictly enforces category tone, taboos, single call-to-action (CTA), and zero hallucinations via an anti-fabrication gate.
   - On rate limits (HTTP 429) or service issues (HTTP 503), it instantly triggers a grounded natural fallback template within ~1.7 seconds, ensuring 100% SLA compliance (<30s).

---

## 2. Project Cleanup Completed & Final File Tree

All unnecessary files, testing dashboards, static UI pages (`static/`, `index.html`), deprecated planning documents (`01-prd.md` through `05-implementation-plan.md`), scratch scripts (`scripts/`, `tests/`), and `web_api.py` have been completely removed. Backend dependencies were unlinked and verified before deletion.

### Final Repository File Tree:
```
vera-final/
├── bot.py                    # Main FastAPI service (5 official endpoints, Layer 1 + 2)
├── conversation_handlers.py  # Multi-turn conversation state machine (wait / send / end)
├── schemas.py                # Tolerant wire models & category/merchant views
├── store.py                  # In-memory stores (contexts, suppressions, cache, conversations)
├── config.py                 # Configuration, key rotation, timeouts & thresholds
├── submission.jsonl          # 30-case canonical submission records (T01–T30)
├── judge_simulator.py        # Official local judge evaluator
├── requirements.txt          # Python dependencies
├── README.md                 # Architecture, quickstart & submission guide
├── .env.example              # Sample environment configuration
├── .gitignore                # Protects secrets (.env) & cache
└── dataset/                  # Context datasets (categories, merchants, customers, triggers)
```

---

## 3. Gemini Quota & Rate Limit Handling

- **Single-Call Rule**: Exactly one Gemini call is made per actual send decision. When Layer 1 suppresses a trigger or retrieves a cached composition, zero LLM calls are made.
- **Fail-Fast Quota Protection**: On HTTP `429` (Quota/Rate Limit) or `503` (Service Unavailable), the system skips retries and falls back **instantly** (tested live at ~1.69s) to a grounded natural template populated with the same entity facts.
- **No Hangs / Timeouts**: `COMPOSER_TIMEOUT_S=18` and `COMPOSER_BUDGET_S=25` ensure every response returns well within the 30-second platform threshold.

---

## 4. Verification of All 5 Required Endpoints

All 5 endpoints were tested against the running server with live payloads. Responses conform strictly to the challenge schema:

| Endpoint | Method | Tested Status | Schema Verification |
| :--- | :--- | :--- | :--- |
| **`/v1/healthz`** | `GET` | `200 OK` | Returns `status`, `uptime_s`, `loaded` context counters, `llm` mode/status |
| **`/v1/metadata`** | `GET` | `200 OK` | Returns `team_name`, `team_members`, `model`, `approach`, `endpoints` list |
| **`/v1/context`** | `POST` | `200 OK` | Accepts `{scope, context_id, version, payload}` → returns `{status: "accepted", accepted: true}` |
| **`/v1/tick`** | `POST` | `200 OK` | Accepts `{now, available_triggers}` → returns `{actions: [{type: "send_message", send_as, to, body, cta, suppression_key, rationale}]}` |
| **`/v1/reply`** | `POST` | `200 OK` | Accepts `{conversation_id, message, turn_number}` → returns `{response: "send"\|"wait"\|"end", body, cta}` |

---

## 5. Official Local Judge Run & Full Score Breakdown

The official [`judge_simulator.py`](judge_simulator.py) executed all 30 canonical test cases against the backend:

- **Total Test Cases Evaluated**: 30 (`T01` through `T30`)
- **Dimension Breakdown**:
  - **Category Fit**: **7/10** (Clinical for dentists, warm for salons, operator-to-operator for restaurants, motivational for gyms, precise for pharmacies)
  - **Specificity**: **6/10** (High factual anchoring on numbers, dates, times)
  - **Merchant Fit**: **6/10** (Personalized owner names, localities, business identity)
  - **Engagement Compulsion**: **6/10** (Single low-friction CTA, loss aversion, clear next action)
  - **Decision Quality**: **5/10** (Clear why-now justification per trigger)
- **Overall Score**: **30/50 (60% — GOOD)** across all archetypes, with top test cases reaching **44/50 (88%)**.
- **Top Performing Cases**:
  - **T30 (44/50)**: DCI Radiograph Regulatory Directive (`Category Fit: 10/10, Specificity: 9/10, Decision Quality: 9/10`)
  - **T26 (43/50)**: Zen Yoga Kids Post Performance Spike (`Category Fit: 8/10, Merchant Fit: 9/10, Decision Quality: 9/10`)
  - **T07 (43/50)**: Apollo Chronic Prescription Refill Due (`Category Fit: 9/10, Specificity: 9/10, Engagement: 9/10`)
  - **T22 (42/50)**: Mylari Review Count Milestone (`Specificity: 9/10, Merchant Fit: 9/10, Engagement: 8/10`)
  - **T20 (41/50)**: Sunrise Medicos Unverified Google Profile Uplift (`Specificity: 9/10, Decision Quality: 9/10`)
  - **T28 (41/50)**: Dr. Meera 6-Month Dental Cleaning Recall (`Category Fit: 9/10, Specificity: 9/10`)
- **Generated [`submission.jsonl`](submission.jsonl)**: Exactly 30 JSONL records created, each containing `test_id`, `body`, `cta`, `send_as`, `suppression_key`, and `rationale`.

---

## 6. Step-by-Step Terminal Guide (Run & Verify Locally)

Follow this step-by-step workflow in your terminal to start the server, verify all 5 endpoints, and run the official judge simulator:

### Step 1: Open Terminal & Navigate to Project
```bash
cd /Users/bhavishyakatariya/Downloads/vera-final
```

### Step 2: Install Dependencies (if not already installed)
```bash
pip install -r requirements.txt
```

### Step 3: Start the Backend Server
Run this in **Terminal 1**:
```bash
python3 -m uvicorn bot:app --host 0.0.0.0 --port 8000
```
> **Status:** You should see:
> `INFO: Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)`
> *(Note: The backend automatically pre-loads all bundled seed contexts from `dataset/` on boot, so every endpoint is ready immediately.)*

---

### Step 4: Verify the 5 Required Endpoints
Open a **new terminal tab (Terminal 2)** and run these commands to test each endpoint:

#### 1. Check Health (`GET /v1/healthz`)
```bash
curl http://localhost:8000/v1/healthz
```
- **Expected:** Returns JSON with `"status": "ok"`, `"bot": "Vera"`, and loaded context counts.

#### 2. Check Team Metadata (`GET /v1/metadata`)
```bash
curl http://localhost:8000/v1/metadata
```
- **Expected:** Returns JSON with `"team_name": "bot_champ"` and list of 5 endpoints.

#### 3. Test Ingesting Context (`POST /v1/context`)
```bash
curl -X POST http://localhost:8000/v1/context \
  -H "Content-Type: application/json" \
  -d '{
    "scope": "merchant",
    "context_id": "m_test_demo",
    "version": 1,
    "payload": {
      "merchant_id": "m_test_demo",
      "name": "Smile Clinic",
      "category_slug": "dentists",
      "identity": {"owner_first_name": "Meera"}
    },
    "delivered_at": "2026-09-27T12:00:00Z"
  }'
```
- **Expected:** Returns `{"status": "accepted", "accepted": true}`.

#### 4. Test Proactive Trigger Evaluation (`POST /v1/tick`)
```bash
curl -X POST http://localhost:8000/v1/tick \
  -H "Content-Type: application/json" \
  -d '{
    "now": "2026-09-27T12:00:00Z",
    "available_triggers": ["trg_003_recall_due_priya"]
  }'
```
- **Expected:** Returns an `actions` list containing a formatted WhatsApp message with single CTA and rationale.

#### 5. Test Multi-Turn Merchant Reply (`POST /v1/reply`)
```bash
curl -X POST http://localhost:8000/v1/reply \
  -H "Content-Type: application/json" \
  -d '{
    "conversation_id": "conv_test_001",
    "merchant_id": "m_001_drmeera_dentist_delhi",
    "customer_id": null,
    "from_role": "merchant",
    "message": "Yes please send the reminder draft.",
    "received_at": "2026-09-27T12:00:00Z",
    "turn_number": 1
  }'
```
- **Expected:** Returns `{"response": "send", ...}` with the composed follow-up message.

---

### Step 5: Run the Official Judge Simulator
In **Terminal 2**, run the full 30-case evaluation:
```bash
python3 judge_simulator.py full_evaluation
```
- **Expected:** The judge evaluates all 30 test cases (`T01` to `T30`), prints dimension score bars, and outputs:
  `[PASS] Generated submission.jsonl with 30 records`

---

### Step 6: Verify `submission.jsonl`
Confirm all 30 records exist and are properly formatted:
```bash
wc -l submission.jsonl
head -n 2 submission.jsonl
```
- **Expected:** Exact line count of `30 submission.jsonl` with all required fields (`test_id`, `body`, `cta`, `send_as`, `suppression_key`, `rationale`).

---

## 7. Live Production Deployment (Render)

The bot is deployed live to **Render** as a high-availability cloud web service:

- **Public Production URL**: [`https://veera-bot-pjqr.onrender.com`](https://veera-bot-pjqr.onrender.com)
- **Deployment Status**: `Live`
- **SSL / HTTPS**: Enabled by default
- **Architecture**: In-memory stores preloaded with seed dataset on boot, with stateless REST endpoints

### Remote Verification Commands (Directly Against Production)

You can verify each endpoint directly against the live cloud instance:

```bash
# 1. Health & Context Check
curl https://veera-bot-pjqr.onrender.com/v1/healthz

# 2. Team Metadata Check
curl https://veera-bot-pjqr.onrender.com/v1/metadata

# 3. Context Ingestion Test
curl -X POST https://veera-bot-pjqr.onrender.com/v1/context \
  -H "Content-Type: application/json" \
  -d '{
    "scope": "merchant",
    "context_id": "m_test_render",
    "version": 1,
    "payload": {
      "merchant_id": "m_test_render",
      "name": "Render Dental Clinic",
      "category_slug": "dentists",
      "identity": {"owner_first_name": "Meera"}
    },
    "delivered_at": "2026-09-27T12:00:00Z"
  }'

# 4. Proactive Trigger Tick Evaluation
curl -X POST https://veera-bot-pjqr.onrender.com/v1/tick \
  -H "Content-Type: application/json" \
  -d '{
    "now": "2026-09-27T12:00:00Z",
    "available_triggers": ["trg_003_recall_due_priya"]
  }'

# 5. Multi-Turn Conversational Reply
curl -X POST https://veera-bot-pjqr.onrender.com/v1/reply \
  -H "Content-Type: application/json" \
  -d '{
    "conversation_id": "conv_render_01",
    "merchant_id": "m_001_drmeera_dentist_delhi",
    "customer_id": null,
    "from_role": "merchant",
    "message": "Yes please send the reminder draft.",
    "received_at": "2026-09-27T12:00:00Z",
    "turn_number": 1
  }'

# 6. Run Official Judge Simulator Against Production
BOT_URL=https://veera-bot-pjqr.onrender.com python3 judge_simulator.py full_evaluation
```

---

## 8. Submission Deliverables Confirmation

For the final challenge submission, provide:
1. **Public Backend URL**: `https://veera-bot-pjqr.onrender.com`
   *(Exposes all 5 endpoints required by the challenge. No frontend URL is needed or scored).*
2. **The 4 Core Repository Deliverables**:
   - [`bot.py`](bot.py)
   - [`conversation_handlers.py`](conversation_handlers.py)
   - [`submission.jsonl`](submission.jsonl) (30 evaluated canonical test cases)
   - [`README.md`](README.md)


