#!/usr/bin/env python3
"""Generate Vera's rehearsal dataset (synthetic dentistry vertical).

⚠ PLACEHOLDER: the real challenge zip (dataset/generate_dataset.py) was not
available at build time. This script fabricates a schema-faithful rehearsal
dataset so the pipeline and submission.jsonl exist end-to-end. When the real
zip arrives, re-run `python scripts/gen_submission.py` against the REAL
canonical 30 pairs — code changes should be limited to field mapping in
schemas.py.

Outputs (dataset/):
  category_dentistry.json  — the CategoryContext (dentistry)
  merchants.json           — 6 merchant contexts
  customers.json           — 6 customer contexts (for customer-directed sends)
  pairs.json               — the 30 canonical (merchant, trigger) test pairs
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "dataset"))
NOW = datetime(2026, 11, 10, 10, 0, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


CATEGORY = {
    "category": "dentistry",
    "vertical": "dental_clinic",
    "voice": {
        "tone": "clinical but warm, peer-to-peer professional; concise; zero hype",
        "style_notes": "short sentences; no exclamation marks; precise, practical next steps; respectful of the merchant's time",
    },
    "taboos": [
        "discount language", "cheap deals", "deal of the day",
        "fear-mongering about dental problems", "guaranteed results claims",
        "more than one ask per message",
    ],
    "offer_catalog": [
        {"offer_id": "ofr_hygiene_slot", "title": "Weekday hygiene slot block",
         "detail": "Extra weekday hygiene slots opened 7-9 AM for working patients",
         "valid_until": "2026-11-30"},
        {"offer_id": "ofr_recall_camp", "title": "Recall week camp",
         "detail": "Reserved slots for six-monthly check-up patients, first week of every month",
         "valid_until": "2026-12-31"},
        {"offer_id": "ofr_school_camp", "title": "School dental camp program",
         "detail": "On-campus check-up camps for partner schools, bookings open",
         "valid_until": "2027-01-31"},
    ],
    "peer_stats": {
        "median_no_show_rate": "18%",
        "top_quartile_no_show": "9%",
        "median_new_reviews_per_month": "9",
        "recall_rebook_benchmark": "46%",
        "lead_callback_benchmark": "82% within 24h",
    },
    "digest": {
        "cadence": "weekly", "best_day": "monday", "window": "10:00-12:00",
        "sections": ["no-show rate vs peer median", "new patient leads", "review score movement"],
    },
    "seasonal_beats": [
        {"beat": "new_year_checkup", "window": "jan", "note": "resolutions drive check-up demand"},
        {"beat": "world_oral_health_day", "window": "mar-20", "note": "awareness content performs well"},
        {"beat": "school_holiday_braces", "window": "may-jun", "note": "parents book ortho consults"},
    ],
    "cta_preferences": [
        "Shall I set it up?", "Want us to go ahead?",
        "Reply YES and we'll hold the slot", "Shall I draft it for your approval?",
        "Want a draft to review first?", "Shall I prepare the list?",
    ],
}

MERCHANTS = [
    {"merchant_id": "m01", "merchant_name": "Bright Smile Dental", "locality": "Indiranagar",
     "stats": {"no_show_rate": "24%", "rating": "4.3", "monthly_footfall": "310",
               "recall_list_size": "96", "new_reviews_this_month": "4"},
     "history": ["no-show rate rising 3 months in a row", "last digest sent 2 weeks ago"]},
    {"merchant_id": "m02", "merchant_name": "Pearl Dental Studio", "locality": "Koramangala",
     "stats": {"no_show_rate": "12%", "rating": "4.7", "total_reviews": "12",
               "monthly_footfall": "85", "new_reviews_this_month": "1",
               "recall_list_size": "38"},
     "history": ["opened 6 months ago", "review count growing slowly"]},
    {"merchant_id": "m03", "merchant_name": "CitySmile Ortho Plus", "locality": "HSR Layout",
     "stats": {"no_show_rate": "16%", "rating": "4.4", "active_braces_cases": "42",
               "consultations_this_week": "11", "recall_list_size": "27",
               "new_reviews_this_month": "3"},
     "history": ["orthodontics-focused practice", "consult-to-start rate improving"]},
    {"merchant_id": "m04", "merchant_name": "GentleCare Family Dental", "locality": "Jayanagar",
     "stats": {"no_show_rate": "15%", "rating": "4.5", "recall_list_size": "180",
               "monthly_footfall": "420", "new_reviews_this_month": "6"},
     "history": ["largest recall list in cluster", "weekend slots fill fastest"]},
    {"merchant_id": "m05", "merchant_name": "Apex Dental & Implant Center", "locality": "Whitefield",
     "stats": {"no_show_rate": "21%", "rating": "4.6", "implants_placed": "500",
               "avg_consult_wait_days": "4", "recall_list_size": "64",
               "new_reviews_this_month": "5"},
     "history": ["recently crossed 500 implants", "premium implant practice"]},
    {"merchant_id": "m06", "merchant_name": "Little Teeth Pediatric Dental", "locality": "Marathahalli",
     "stats": {"no_show_rate": "11%", "rating": "4.8", "partner_schools": "6",
               "monthly_footfall": "260", "recall_list_size": "52",
               "new_reviews_this_month": "7"},
     "history": ["pediatric specialist", "school camp program expanding"]},
]

CUSTOMERS = [
    {"customer_id": "c01", "customer_name": "R. Mehta", "merchant_id": "m01",
     "history": {"last_visit": "2026-08-01", "missed": "cleaning slot yesterday", "visits_total": "5"}},
    {"customer_id": "c02", "customer_name": "A. Sharma", "merchant_id": "m03",
     "history": {"enquiry": "braces consultation request via magicpin", "requested": "callback today"}},
    {"customer_id": "c03", "customer_name": "P. Iyer", "merchant_id": "m06",
     "history": {"enquiry": "first dental visit for 6-year-old", "requested": "weekend slot"}},
    {"customer_id": "c04", "customer_name": "S. Khan", "merchant_id": "m04",
     "history": {"last_visit": "2026-07-12", "missed": "recall check-up last week", "visits_total": "3"}},
    {"customer_id": "c05", "customer_name": "D. Patel", "merchant_id": "m02",
     "history": {"enquiry": "teeth whitening pricing", "requested": "price list"}},
    {"customer_id": "c06", "customer_name": "K. Reddy", "merchant_id": "m05",
     "history": {"last_visit": "2026-09-02", "missed": "implant follow-up yesterday", "visits_total": "7"}},
]


def _plural_reviews(n: str) -> str:
    try:
        return "review" if int(n) == 1 else "reviews"
    except (TypeError, ValueError):
        return "reviews"


# per-merchant recall phrasings (facts: own recall_list_size + category camp offer)
_RECALL_PHRASINGS = {
    "m01": "Your recall list stands at {n} patients due for six-monthly check-ups this week; the Recall week camp holds reserved slots in the first week of the month.",
    "m02": "{n} recall patients are due for their six-monthly check-up this week — the Recall week camp's reserved slots in the first week of the month can absorb them.",
    "m03": "{n} ortho patients on your recall list are due for check-ups this week; Recall week camp slots in the first week of the month are open for booking.",
    "m04": "{n} recall patients are due for their six-monthly check-up this week; the Recall week camp holds reserved slots in the first week of the month.",
    "m05": "Your recall list has {n} patients due this week; Recall week camp slots in the first week of the month can take the overflow.",
    "m06": "{n} young patients on your recall list are due for check-ups this week; the Recall week camp holds reserved slots in the first week of the month.",
}

# per-merchant offer-expiry deadlines (days from NOW), aligned with expires_at
_OFFER_EXPIRY_DAYS = {"m01": 2, "m02": 5, "m03": 9, "m04": 1, "m05": 12, "m06": 6}

# per-merchant digest lead section (grounded in own stats + category peer stats)
_DIGEST_LEADS = {
    "m01": "no-show rate moved to 24% against the 18% peer median",
    "m02": "new reviews sit at 1 this month against the peer median of 9",
    "m03": "11 consultations are in the pipeline for this week",
    "m04": "180 recall patients are due and weekend slots are filling fastest",
    "m05": "average consult wait stands at 4 days",
    "m06": "leads from your 6 partner schools are up for follow-up",
}


def build_pairs() -> list[dict]:
    """6 merchants x 5 trigger archetypes = 30 canonical pairs.

    Every reason is grounded in the merchant's OWN stats or the category
    offer catalog — no cross-merchant number reuse.
    """
    pairs = []
    for m in MERCHANTS:
        mid = m["merchant_id"]
        stats = m["stats"]
        base = [
            {"trigger_type": "recall_due", "urgency": "medium",
             "reason": _RECALL_PHRASINGS[mid].format(n=stats.get("recall_list_size", "0"))},
            {"trigger_type": "offer_expiry", "urgency": "high",
             "reason": ("The Weekday hygiene slot block (7-9 AM) is bookable until 30 Nov — "
                        f"{_OFFER_EXPIRY_DAYS[mid]} days left to fill the new early slots."),
             "expires_at": iso(NOW + timedelta(days=_OFFER_EXPIRY_DAYS[mid], hours=2))},
            {"trigger_type": "review_stagnation", "urgency": "medium",
             "reason": (f"Only {stats.get('new_reviews_this_month', '2')} new Google "
                        f"{_plural_reviews(stats.get('new_reviews_this_month', '2'))} this month "
                        f"against a category median of 9; the review flow needs steady movement.")},
        ]
        customer_triggers = []
        cust_for = [c for c in CUSTOMERS if c["merchant_id"] == mid]
        if cust_for:
            c = cust_for[0]
            hist = c.get("history", {})
            if "missed" in hist:
                customer_triggers.append(
                    {"trigger_type": "no_show_recovery", "urgency": "high", "customer_id": c["customer_id"],
                     "reason": (f"{c['customer_name']} missed their appointment ({hist['missed']}) — "
                                f"a rebook message from the clinic today usually recovers the slot.")})
            elif "enquiry" in hist:
                customer_triggers.append(
                    {"trigger_type": "new_lead", "urgency": "high", "customer_id": c["customer_id"],
                     "reason": (f"{c['customer_name']} sent an enquiry — {hist['enquiry']} "
                                f"— and asked for {hist['requested']}.")})
        fifth = {"trigger_type": "digest_ready", "urgency": "medium",
                 "reason": (f"Monday weekly digest is ready — top line: {_DIGEST_LEADS[mid]}. "
                            "Full sections cover new patient leads and review movement for last week.")}
        for extra in customer_triggers:
            base.append(extra)
        while len(base) < 4:
            base.append(dict(fifth))
        if len(base) == 4:
            base.append(dict(fifth))
        for t in base[:5]:
            tid = f"{mid}_{t['trigger_type']}"
            payload = {"trigger_id": tid, "merchant_id": mid, **t}
            pairs.append({"test_id": f"dentistry__{mid}__{t['trigger_type']}",
                          "merchant_id": mid, "trigger_id": tid,
                          "customer_id": t.get("customer_id"),
                          "trigger_payload": payload})
    return pairs


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    files = {
        "category_dentistry.json": CATEGORY,
        "merchants.json": MERCHANTS,
        "customers.json": CUSTOMERS,
        "pairs.json": build_pairs(),
    }
    for name, data in files.items():
        with open(os.path.join(OUT, name), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        print(f"wrote dataset/{name} ({len(data) if isinstance(data, list) else 1} entries)")


if __name__ == "__main__":
    main()
