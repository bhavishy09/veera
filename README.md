# Vera — magicpin AI Challenge (final package)

This bundle contains everything built for the magicpin merchant-WhatsApp bot
challenge, in two parts:

```
vera-final/
├── vera-submission/      ← THE DELIVERABLE (run this)
│   ├── bot.py                    FastAPI: /v1/context /v1/tick /v1/reply
│   │                             /v1/healthz /v1/metadata (+ /v1/teardown)
│   │                             Layer 1 decision engine + Layer 2 composer
│   ├── conversation_handlers.py  multi-turn reply state machine
│   ├── schemas.py                tolerant wire parsing (single adapter point)
│   ├── store.py                  in-memory stores (idempotent contexts, cache,
│   │                             suppressions, conversations, decision log)
│   ├── config.py                 env config + Layer-1 knobs
│   ├── submission.jsonl          30-line static submission
│   ├── dataset/                  rehearsal dataset (6 merchants x 5 archetypes)
│   ├── scripts/                  gen_dataset / gen_submission / mock_judge /
│   │                             validate_submission
│   ├── tests/                    18 unit tests
│   └── README.md                 full approach + run instructions
└── vera-planning-docs/   ← design docs (PRD, tech stack, flows, schema, plan)
```

## Quickstart

```bash
cd vera-submission
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port $PORT
```

Validate everything locally:

```bash
python -m pytest tests/ -q                 # 18/18 unit tests
python scripts/mock_judge.py               # 36/36 lifecycle checks
python scripts/validate_submission.py      # submission.jsonl hard-rule gate
python scripts/gen_submission.py --live    # regenerate via Gemini Flash (optional)
```

## Before you submit

1. Fill team identity: `TEAM_NAME`, `TEAM_MEMBERS`, `CONTACT_EMAIL`
   (in `.env` / environment — served by `GET /v1/metadata`; defaults are
   `«...»` placeholders).
2. Set `GEMINI_API_KEYS` (2–3 comma-separated keys recommended).
3. If the official challenge zip becomes available: adapt `schemas.py` only
   (single wire-shape adapter point), re-run `gen_submission.py` against the
   official canonical pairs, and run the official `judge_simulator.py`.
   See the Provenance Note in `vera-submission/README.md`.
# veera
