# Vera — magicpin AI Challenge

Vera is an intelligent WhatsApp engagement bot for magicpin merchants and customers. It uses a **two-layer architecture**:
1. **Layer 1 (Decision Engine)**: Deterministic business rules, urgency scoring, silence-bar filtering, deduplication, and suppression key tracking.
2. **Layer 2 (Composition Layer)**: Single-call generative composition powered by Gemini (`gemini-3.1-flash-lite`) with strict schema validation, anti-fabrication gates, taboo filtering, and immediate grounded fallbacks on rate limits or service unavailability.

---

## File Structure

```
.
├── bot.py                    # FastAPI application exposing the 5 required endpoints
├── conversation_handlers.py  # Multi-turn conversation state machine (wait / send / end)
├── schemas.py                # Tolerant wire adapters for context payloads
├── store.py                  # In-memory stores for contexts, deduplication, and suppressions
├── config.py                 # Configuration, key rotation, timeouts, and thresholds
├── submission.jsonl          # 30-case canonical submission records
├── judge_simulator.py        # Official challenge judge simulator and evaluation tool
├── dataset/                  # Merchant, customer, trigger, and category datasets
├── requirements.txt          # Python dependencies (fastapi, uvicorn, pydantic, httpx)
├── .env.example              # Sample environment configuration
└── README.md                 # Project documentation and submission details
```

---

## Required Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/v1/healthz` | Bot status, uptime, loaded context counts, and active LLM configuration |
| `GET` | `/v1/metadata` | Team identity (`bot_champ`), model details, and architectural approach |
| `POST` | `/v1/context` | Ingestion endpoint for categories, merchants, customers, and triggers |
| `POST` | `/v1/tick` | Evaluates triggers and returns proactive WhatsApp messages (`actions`) |
| `POST` | `/v1/reply` | Multi-turn conversational replies from merchants or customers |

---

## Quickstart & Local Execution

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure Environment
Create a `.env` file from `.env.example`:
```bash
cp .env.example .env
```
Ensure your Gemini API key is configured:
```bash
GEMINI_API_KEYS=your_gemini_api_key_here
GEMINI_MODEL=gemini-3.1-flash-lite
```

### 3. Start the Backend Server
```bash
python3 -m uvicorn bot:app --host 0.0.0.0 --port 8000
```

### 4. Run the Official Judge Simulator
In a separate terminal:
```bash
python3 judge_simulator.py full_evaluation
```
This runs the full test suite against all 30 canonical test cases, scores the responses across all 5 evaluation dimensions, and generates `submission.jsonl`.

---

## Submission Details

- **Deliverables**:
  - `bot.py`
  - `conversation_handlers.py`
  - `submission.jsonl` (30 canonical test cases with `test_id`, `body`, `cta`, `send_as`, `suppression_key`, `rationale`)
  - `README.md`
  - **Public Backend URL** exposing the 5 endpoints listed above (pure HTTP backend, no frontend required)
