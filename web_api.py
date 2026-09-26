"""Web UI & Interactive Simulation API for Vera.

Exposes endpoints for the browser dashboard:
- GET / : Interactive Dashboard UI
- GET /api/dataset : All categories, merchants, customers, triggers & preset scenarios
- POST /api/simulate : Live trigger execution with WhatsApp preview & timing
- POST /api/judge : Run the official 5-dimension LLM judge on any generated message
- GET /api/results : Benchmark results & scoring distribution
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

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


def _load_catalog():
    """Load categories, merchants, customers, triggers, test pairs & presets across all 5 verticals."""
    cats_dir = ROOT / "dataset" / "categories"
    categories: List[Dict[str, Any]] = []
    category_map: Dict[str, Any] = {}

    icons = {
        "dentists": "🦷",
        "salons": "💇‍♀️",
        "restaurants": "🍛",
        "gyms": "🏋️‍♂️",
        "pharmacies": "💊",
    }

    if cats_dir.exists():
        for f in sorted(cats_dir.glob("*.json")):
            slug = f.stem
            data = _load_json(f"dataset/categories/{f.name}")
            if not data:
                continue
            cat_info = {
                "slug": slug,
                "name": data.get("display_name") or data.get("category", slug.title()),
                "icon": icons.get(slug, "🏪"),
                "tone": (data.get("voice") or {}).get("tone", ""),
                "taboos": (data.get("voice") or {}).get("vocab_taboo") or data.get("taboos") or [],
                "offers": (data.get("offer_catalog") or [])[:3],
                "peer_stats": data.get("peer_stats") or {},
                "raw": data,
            }
            categories.append(cat_info)
            category_map[slug] = data

    merchants_map: Dict[str, Any] = {}
    seeds_m = (_load_json("dataset/merchants_seed.json") or {}).get("merchants", [])
    for m in seeds_m:
        mid = m.get("merchant_id") or m.get("id")
        if mid:
            merchants_map[mid] = m

    exp_m_dir = ROOT / "dataset" / "expanded" / "merchants"
    if exp_m_dir.is_dir():
        for f in sorted(exp_m_dir.glob("*.json")):
            d = _load_json(f"dataset/expanded/merchants/{f.name}")
            if d:
                mid = d.get("merchant_id") or d.get("id") or f.stem
                if mid and mid not in merchants_map:
                    merchants_map[mid] = d

    customers_map: Dict[str, Any] = {}
    seeds_c = (_load_json("dataset/customers_seed.json") or {}).get("customers", [])
    for c in seeds_c:
        cid = c.get("customer_id") or c.get("id")
        if cid:
            customers_map[cid] = c

    exp_c_dir = ROOT / "dataset" / "expanded" / "customers"
    if exp_c_dir.is_dir():
        for f in sorted(exp_c_dir.glob("*.json")):
            d = _load_json(f"dataset/expanded/customers/{f.name}")
            if d:
                cid = d.get("customer_id") or d.get("id") or f.stem
                if cid and cid not in customers_map:
                    customers_map[cid] = d

    triggers_map: Dict[str, Any] = {}
    seeds_t = (_load_json("dataset/triggers_seed.json") or {}).get("triggers", [])
    for t in seeds_t:
        tid = t.get("id") or t.get("trigger_id")
        if tid:
            triggers_map[tid] = t

    exp_t_dir = ROOT / "dataset" / "expanded" / "triggers"
    if exp_t_dir.is_dir():
        for f in sorted(exp_t_dir.glob("*.json")):
            d = _load_json(f"dataset/expanded/triggers/{f.name}")
            if d:
                tid = d.get("id") or d.get("trigger_id") or f.stem
                if tid and tid not in triggers_map:
                    triggers_map[tid] = d

    pairs_raw = (_load_json("dataset/expanded/test_pairs.json") or {}).get("pairs", [])
    test_pairs = []
    for p in pairs_raw:
        m = merchants_map.get(p.get("merchant_id"), {})
        test_pairs.append({
            "test_id": p.get("test_id"),
            "category_slug": m.get("category_slug", "dentists"),
            "merchant_id": p.get("merchant_id"),
            "customer_id": p.get("customer_id"),
            "trigger_id": p.get("trigger_id"),
        })

    presets = [
        # 1. DENTISTS
        {
            "id": "preset_priya_recall",
            "category_slug": "dentists",
            "title": "🦷 Dental Cleaning Recall (Customer Facing)",
            "subtitle": "Dr. Meera's Clinic → Priya (6-mo recall with 2 evening slots)",
            "merchant_id": "m_001_drmeera_dentist_delhi",
            "customer_id": "c_001_priya_for_m001",
            "trigger_id": "trg_003_recall_due_priya",
        },
        {
            "id": "preset_dci_compliance",
            "category_slug": "dentists",
            "title": "📋 DCI Radiograph Mandate (Merchant Facing)",
            "subtitle": "Platform → Dr. Meera (15 Dec deadline compliance alert)",
            "merchant_id": "m_001_drmeera_dentist_delhi",
            "customer_id": None,
            "trigger_id": "trg_002_compliance_dci_radiograph",
        },
        # 2. SALONS
        {
            "id": "preset_salon_bridal",
            "category_slug": "salons",
            "title": "💇‍♀️ 30-Day Bridal Skin Prep (Customer Facing)",
            "subtitle": "Studio11 Salon → Kavya (Wedding 8 Nov follow-up)",
            "merchant_id": "m_003_studio11_salon_hyderabad",
            "customer_id": "c_005_kavya_for_m003",
            "trigger_id": "trg_007_bridal_followup_kavya",
        },
        {
            "id": "preset_festival_diwali",
            "category_slug": "salons",
            "title": "🪔 Diwali Festive Booking Window (Merchant Facing)",
            "subtitle": "Platform → Studio11 Salon (Diwali upcoming in 188 days)",
            "merchant_id": "m_003_studio11_salon_hyderabad",
            "customer_id": None,
            "trigger_id": "trg_006_festival_diwali",
        },
        # 3. RESTAURANTS
        {
            "id": "preset_corp_thali",
            "category_slug": "restaurants",
            "title": "🍛 Corporate Bulk Lunch Thali (Merchant Facing)",
            "subtitle": "Platform → Mylari South Indian Cafe (Active corporate meal planning)",
            "merchant_id": "m_006_southindiancafe_restaurant_bangalore",
            "customer_id": None,
            "trigger_id": "trg_013_corporate_thali_planning",
        },
        {
            "id": "preset_ipl_match",
            "category_slug": "restaurants",
            "title": "🍕 IPL Match Night Delivery Surge (Merchant Facing)",
            "subtitle": "Platform → SK Pizza Junction (DC vs MI tonight at Arun Jaitley)",
            "merchant_id": "m_005_pizzajunction_restaurant_delhi",
            "customer_id": None,
            "trigger_id": "trg_010_ipl_match_delhi",
        },
        # 4. GYMS
        {
            "id": "preset_kids_yoga",
            "category_slug": "gyms",
            "title": "🧘 Kids Yoga Summer Camp Drafting (Merchant Facing)",
            "subtitle": "Platform → Zen Yoga Studio (Summer program curriculum & timings)",
            "merchant_id": "m_008_zenyoga_gym_chennai",
            "customer_id": None,
            "trigger_id": "trg_016_kids_yoga_program_drafting",
        },
        {
            "id": "preset_gym_winback",
            "category_slug": "gyms",
            "title": "🏋️‍♂️ Winback Lapsed Member (Customer Facing)",
            "subtitle": "PowerHouse Fitness → Rashmi (57 days lapsed, weight loss focus)",
            "merchant_id": "m_007_powerhouse_gym_bangalore",
            "customer_id": "c_010_rashmi_for_m007",
            "trigger_id": "trg_015_winback_rashmi",
        },
        # 5. PHARMACIES
        {
            "id": "preset_chronic_refill",
            "category_slug": "pharmacies",
            "title": "💊 Chronic Medication Refill Due (Customer Facing)",
            "subtitle": "Apollo Pharmacy → Grandfather (Metformin/Atorvastatin runs out 28 Apr)",
            "merchant_id": "m_009_apollo_pharmacy_jaipur",
            "customer_id": "c_013_grandfather_for_m009",
            "trigger_id": "trg_019_chronic_refill_grandfather",
        },
        {
            "id": "preset_batch_recall",
            "category_slug": "pharmacies",
            "title": "⚠️ Urgent Drug Recall Notice (Merchant Facing)",
            "subtitle": "Platform → Apollo Pharmacy (Atorvastatin batches AT2024-1102 / 1108)",
            "merchant_id": "m_009_apollo_pharmacy_jaipur",
            "customer_id": None,
            "trigger_id": "trg_018_supply_atorvastatin_recall",
        },
    ]

    return {
        "categories": categories,
        "category_map": category_map,
        "merchants_map": merchants_map,
        "customers_map": customers_map,
        "triggers_map": triggers_map,
        "presets": presets,
        "test_pairs": test_pairs,
    }


# In-memory cache loaded once on import
CATALOG = _load_catalog()


@router.get("/api/dataset")
def get_dataset():
    """Return catalog of all categories, merchants, customers, triggers & presets for the UI."""
    return {
        "categories": CATALOG["categories"],
        "merchants": list(CATALOG["merchants_map"].values()),
        "customers": list(CATALOG["customers_map"].values()),
        "triggers": list(CATALOG["triggers_map"].values()),
        "presets": CATALOG["presets"],
        "test_pairs": CATALOG["test_pairs"],
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

    # Load Category
    cat_data = CATALOG["category_map"].get(req.category_slug)
    if not cat_data:
        cat_file = ROOT / "dataset" / "categories" / f"{req.category_slug}.json"
        if cat_file.exists():
            cat_data = _load_json(f"dataset/categories/{req.category_slug}.json")
            CATALOG["category_map"][req.category_slug] = cat_data
    if not cat_data:
        return JSONResponse({"error": f"Category {req.category_slug} not found"}, status_code=404)

    # Load Merchant
    merch_data = CATALOG["merchants_map"].get(req.merchant_id)
    if not merch_data:
        exp_m_path = ROOT / "dataset" / "expanded" / "merchants" / f"{req.merchant_id}.json"
        if exp_m_path.exists():
            merch_data = _load_json(f"dataset/expanded/merchants/{req.merchant_id}.json")
            CATALOG["merchants_map"][req.merchant_id] = merch_data
    if not merch_data:
        return JSONResponse({"error": f"Merchant {req.merchant_id} not found"}, status_code=404)

    # Load Customer if requested
    cust_data = None
    if req.customer_id:
        cust_data = CATALOG["customers_map"].get(req.customer_id)
        if not cust_data:
            exp_c_path = ROOT / "dataset" / "expanded" / "customers" / f"{req.customer_id}.json"
            if exp_c_path.exists():
                cust_data = _load_json(f"dataset/expanded/customers/{req.customer_id}.json")
                CATALOG["customers_map"][req.customer_id] = cust_data

    # Load Trigger
    trig_data = CATALOG["triggers_map"].get(req.trigger_id)
    if not trig_data:
        exp_t_path = ROOT / "dataset" / "expanded" / "triggers" / f"{req.trigger_id}.json"
        if exp_t_path.exists():
            trig_data = _load_json(f"dataset/expanded/triggers/{req.trigger_id}.json")
            CATALOG["triggers_map"][req.trigger_id] = trig_data
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
