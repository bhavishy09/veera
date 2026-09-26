"""Web UI & Interactive Simulation API for Vera.

Exposes endpoints for the browser dashboard:
- GET / : Interactive Dashboard UI
- GET /api/dataset : Categories, merchants, customers, triggers & preset scenarios
- POST /api/simulate : Live trigger execution with WhatsApp preview & timing
- POST /api/judge : Run the official 5-dimension LLM judge on any generated message
- GET /api/results : Benchmark results & scoring distribution
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import bot
from schemas import CategoryView, CustomerView, MerchantView, TriggerView
from store import STORE

router = APIRouter()
ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"


def _load_json(rel_path: str) -> Any:
    p = ROOT / rel_path
    if not p.exists():
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


@router.get("/api/dataset")
def get_dataset():
    """Return catalog of categories, merchants, customers, triggers & presets for the UI."""
    cats_dir = ROOT / "dataset" / "categories"
    categories = []
    if cats_dir.exists():
        for f in sorted(cats_dir.glob("*.json")):
            data = _load_json(f"dataset/categories/{f.name}")
            slug = f.stem
            categories.append({
                "slug": slug,
                "name": data.get("category", slug.title()),
                "tone": (data.get("voice") or {}).get("tone", ""),
                "taboos": data.get("taboos") or [],
                "offers": (data.get("offer_catalog") or [])[:3],
                "peer_stats": data.get("peer_stats") or {},
            })

    merchants_raw = (_load_json("dataset/merchants_seed.json") or {}).get("merchants", [])
    customers_raw = (_load_json("dataset/customers_seed.json") or {}).get("customers", [])
    triggers_raw = (_load_json("dataset/triggers_seed.json") or {}).get("triggers", [])

    presets = [
        {
            "id": "preset_priya_recall",
            "title": "🦷 Dental Cleaning Recall (Customer Facing)",
            "subtitle": "Dr. Meera's Clinic → Priya (6-mo recall with 2 evening slots)",
            "category_slug": "dentists",
            "merchant_id": "m_001_drmeera_dentist_delhi",
            "customer_id": "c_001_priya_for_m001",
            "trigger_id": "trg_003_recall_due_priya",
        },
        {
            "id": "preset_dci_compliance",
            "title": "📋 DCI Radiograph Mandate (Merchant Facing)",
            "subtitle": "Platform → Dr. Meera (15 Dec deadline compliance alert)",
            "category_slug": "dentists",
            "merchant_id": "m_001_drmeera_dentist_delhi",
            "customer_id": None,
            "trigger_id": "trg_002_compliance_dci_radiograph",
        },
        {
            "id": "preset_salon_bridal",
            "title": "💇‍♀️ 30-Day Bridal Skin Prep (Customer Facing)",
            "subtitle": "Studio11 Salon → Kavya (Wedding 8 Nov follow-up)",
            "category_slug": "salons",
            "merchant_id": "m_003_studio11_salon_hyderabad",
            "customer_id": "c_005_kavya_for_m003",
            "trigger_id": "trg_007_bridal_followup_kavya",
        },
        {
            "id": "preset_dentist_dip",
            "title": "📉 Weekly Calls Drop -50% (Merchant Facing)",
            "subtitle": "Platform → Dr. Bharat (Inbound calls dropped vs 12 baseline)",
            "category_slug": "dentists",
            "merchant_id": "m_002_bharat_dentist_mumbai",
            "customer_id": None,
            "trigger_id": "trg_004_perf_dip_bharat",
        },
        {
            "id": "preset_pro_renewal",
            "title": "⏳ Pro Plan Renewal (12 Days Left)",
            "subtitle": "Platform → Dr. Bharat (₹4999 renewal upcoming)",
            "category_slug": "dentists",
            "merchant_id": "m_002_bharat_dentist_mumbai",
            "customer_id": None,
            "trigger_id": "trg_005_renewal_due_bharat",
        },
        {
            "id": "preset_festival_diwali",
            "title": "🪔 Diwali Festive Booking Window",
            "subtitle": "Platform → Studio11 Salon (Diwali upcoming in 188 days)",
            "category_slug": "salons",
            "merchant_id": "m_003_studio11_salon_hyderabad",
            "customer_id": None,
            "trigger_id": "trg_006_festival_diwali",
        },
    ]

    return {
        "categories": categories,
        "merchants": merchants_raw,
        "customers": customers_raw,
        "triggers": triggers_raw,
        "presets": presets,
    }


class SimulateRequest(BaseModel):
    category_slug: str
    merchant_id: str
    trigger_id: str
    customer_id: Optional[str] = None
    override_now: Optional[str] = None


@router.post("/api/simulate")
def simulate_trigger(req: SimulateRequest):
    """Run an end-to-end live trigger through Vera and return formatted WhatsApp output."""
    t0 = time.time()

    # Load resources
    cat_data = _load_json(f"dataset/categories/{req.category_slug}.json")
    if not cat_data:
        return JSONResponse({"error": f"Category {req.category_slug} not found"}, status_code=404)

    merchants_raw = (_load_json("dataset/merchants_seed.json") or {}).get("merchants", [])
    merch_data = next((m for m in merchants_raw if m.get("merchant_id") == req.merchant_id or m.get("id") == req.merchant_id), None)
    if not merch_data:
        return JSONResponse({"error": f"Merchant {req.merchant_id} not found"}, status_code=404)

    cust_data = None
    if req.customer_id:
        customers_raw = (_load_json("dataset/customers_seed.json") or {}).get("customers", [])
        cust_data = next((c for c in customers_raw if c.get("customer_id") == req.customer_id or c.get("id") == req.customer_id), None)

    triggers_raw = (_load_json("dataset/triggers_seed.json") or {}).get("triggers", [])
    trig_data = next((t for t in triggers_raw if t.get("id") == req.trigger_id), None)
    if not trig_data:
        return JSONResponse({"error": f"Trigger {req.trigger_id} not found"}, status_code=404)

    # Ingest contexts into STORE
    STORE.upsert_context("category", req.category_slug, 1, cat_data)
    STORE.upsert_context("merchant", req.merchant_id, 1, merch_data)
    if cust_data and req.customer_id:
        STORE.upsert_context("customer", req.customer_id, 1, cust_data)
    STORE.upsert_context("trigger", req.trigger_id, 1, trig_data)

    cat = CategoryView(cat_data)
    merch = MerchantView(merch_data)
    cust = CustomerView(cust_data) if cust_data else None
    trig = TriggerView(trig_data, req.trigger_id)

    comp, source = bot.compose(cat, merch, trig, cust, req.merchant_id)
    latency_ms = (time.time() - t0) * 1000

    send_as = "merchant" if (cust is not None and trig.customer_id) else "platform"
    recipient = req.customer_id if send_as == "merchant" else req.merchant_id

    action = {
        "type": "send_message",
        "send_as": send_as,
        "to": recipient,
        "body": comp.get("body", ""),
        "cta": comp.get("cta", ""),
        "suppression_key": comp.get("suppression_key", ""),
        "rationale": comp.get("rationale", ""),
        "trigger_id": req.trigger_id,
        "merchant_id": req.merchant_id,
        "customer_id": req.customer_id,
        "source": source,
    }

    return {
        "action": action,
        "latency_ms": round(latency_ms, 1),
        "source": source,
        "merchant": merch_data,
        "customer": cust_data,
        "trigger": trig_data,
        "category": {"slug": req.category_slug, "name": cat.name, "tone": cat.tone},
    }


class JudgeRequest(BaseModel):
    action: Dict[str, Any]
    category_slug: str
    merchant_id: str
    trigger_id: str
    customer_id: Optional[str] = None


@router.post("/api/judge")
def judge_action(req: JudgeRequest):
    """Run the official 5-dimension LLM judge on any action."""
    try:
        from judge_simulator import DatasetLoader, LLMScorer, create_provider
        dataset = DatasetLoader(ROOT / "dataset")
        dataset.load()
        llm = create_provider()
        scorer = LLMScorer(llm, dataset)

        cat = dataset.categories.get(req.category_slug, {})
        merch = dataset.merchants.get(req.merchant_id, {})
        cust = dataset.customers.get(req.customer_id) if req.customer_id else None
        trig = dataset.triggers.get(req.trigger_id, {})

        score = scorer.score(req.action, cat, merch, trig, cust)
        return {
            "total": score.total,
            "max": 50,
            "dimensions": {
                "specificity": {"score": score.specificity, "reason": score.specificity_reason},
                "category_fit": {"score": score.category_fit, "reason": score.category_fit_reason},
                "merchant_fit": {"score": score.merchant_fit, "reason": score.merchant_fit_reason},
                "decision_quality": {"score": score.decision_quality, "reason": score.decision_quality_reason},
                "engagement_compulsion": {"score": score.engagement_compulsion, "reason": score.engagement_reason},
            },
            "penalties": score.penalties,
            "penalty_reasons": score.penalty_reasons,
            "hint": score.hint,
        }
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/results")
def get_benchmark_results():
    """Return historical or current results.json."""
    res = _load_json("results.json")
    if not res:
        return {"status": "none", "results": []}
    return res


@router.get("/", response_class=HTMLResponse)
def index_page():
    """Serve the single-page interactive dashboard."""
    html_file = STATIC_DIR / "index.html"
    if html_file.exists():
        return html_file.read_text(encoding="utf-8")
    return "<h1>Vera Frontend Loading...</h1>"
