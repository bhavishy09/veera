"""Vera — unit test suite (offline; no network, no LLM).

Run:  cd vera && DISABLE_LLM=1 python -m pytest tests/ -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISABLE_LLM", "1")

import pytest
from fastapi.testclient import TestClient  # noqa: E402

import bot  # noqa: E402
from conversation_handlers import respond  # noqa: E402
from schemas import CategoryView, CustomerView, MerchantView, TriggerView  # noqa: E402
from store import STORE  # noqa: E402

CATEGORY = {
    "category": "dentistry",
    "voice": {"tone": "clinical but warm, peer-to-peer; concise; no hype",
              "style_notes": "short sentences, no exclamation marks"},
    "taboos": ["discount language", "cheap deals", "guaranteed results claims"],
    "offer_catalog": [
        {"offer_id": "ofr_hygiene", "title": "Weekday hygiene slot block",
         "detail": "Extra hygiene slots 7-9 AM for working patients",
         "valid_until": "2030-01-01"},
    ],
    "peer_stats": {"median_no_show_rate": "18%", "recall_rebook_benchmark": "46%"},
    "digest": {"cadence": "weekly", "best_day": "monday"},
    "seasonal_beats": [{"beat": "new_year_checkup", "window": "jan"}],
    "cta_preferences": ["Shall I set it up?", "Want us to go ahead?"],
}

MERCHANT = {
    "merchant_id": "m01",
    "merchant_name": "Bright Smile Dental",
    "stats": {"no_show_rate": "24%", "rating": "4.3", "monthly_footfall": "310"},
    "history": ["last digest 2 weeks ago"],
}

CUSTOMER = {
    "customer_id": "c01",
    "customer_name": "R. Mehta",
    "last_visit": "2026-08-01",
}


def fresh_store():
    STORE.reset()
    return STORE


def push_category(store):
    store.upsert_context("category", "cat_dent", 1, CATEGORY)


def push_merchant(store, mid="m01"):
    m = dict(MERCHANT)
    m["merchant_id"] = mid
    store.upsert_context("merchant", mid, 1, m)


def push_trigger(store, tid, payload_overrides=None, version=1):
    payload = {"trigger_id": tid, "trigger_type": "recall_due",
               "reason": "12 recall patients are due for their six-monthly check-up this week.",
               "merchant_id": "m01", "urgency": "medium"}
    if payload_overrides:
        payload.update(payload_overrides)
    store.upsert_context("trigger", tid, version, payload)


def make_views(tid="t_test"):
    return (CategoryView(CATEGORY), MerchantView(MERCHANT), CustomerView(CUSTOMER),
            TriggerView({"trigger_id": tid, "trigger_type": "recall_due",
                         "reason": "12 recall patients are due this week.",
                         "urgency": "medium"}, tid))


# ---------------------------------------------------------------- idempotency
def test_context_idempotency_same_version_noop():
    store = fresh_store()
    ok1, new1 = store.upsert_context("merchant", "m01", 3, MERCHANT)
    ok2, new2 = store.upsert_context("merchant", "m01", 3, MERCHANT)
    ok3, _ = store.upsert_context("merchant", "m01", 2, MERCHANT)
    assert ok1 and new1
    assert not ok2 and not new2          # replay acknowledged, no mutation
    assert not ok3                       # older version ignored
    assert store.counts()["replays_acknowledged"] == 2


def test_context_newer_version_replaces():
    store = fresh_store()
    store.upsert_context("merchant", "m01", 1, MERCHANT)
    m2 = dict(MERCHANT, rating="4.6")
    ok, new = store.upsert_context("merchant", "m01", 2, m2)
    assert ok and new
    assert store.get_merchant("m01")["rating"] == "4.6"


# ---------------------------------------------------------------- layer 1
def test_expired_trigger_never_composes():
    store = fresh_store()
    push_category(store); push_merchant(store)
    push_trigger(store, "t_exp", {"expires_at": "2001-01-01T00:00:00Z"})
    actions = bot.layer1_evaluate(1_900_000_000.0)
    assert actions == []
    assert store.triggers["t_exp"].status == "expired"


def test_suppressed_trigger_never_composes():
    store = fresh_store()
    push_category(store); push_merchant(store)
    push_trigger(store, "t_sup")
    store.register_suppression("trigger:t_sup", "composed", "m01", "t_sup")
    actions = bot.layer1_evaluate(1_900_000_000.0)
    assert actions == []


def test_no_merchant_context_is_silence():
    store = fresh_store()
    push_category(store)
    push_trigger(store, "t_nom")     # merchant m01 never pushed
    actions = bot.layer1_evaluate(1_900_000_000.0)
    assert actions == []


def test_happy_path_single_send_then_silence():
    store = fresh_store()
    push_category(store); push_merchant(store)
    push_trigger(store, "t_happy")
    now = 1_900_000_000.0
    actions = bot.layer1_evaluate(now)
    assert len(actions) == 1
    a = actions[0]
    assert a["body"] and a["cta"] and a["suppression_key"] and a["rationale"]
    assert "http" not in a["body"].lower()
    # replay of same trigger -> still silence (cache + suppression)
    store.triggers["t_happy"].status = "open"
    actions2 = bot.layer1_evaluate(now + 60)
    assert actions2 == []


def test_low_value_triggers_produce_silence():
    store = fresh_store()
    push_category(store); push_merchant(store)
    push_trigger(store, "t_low", {"urgency": "low",
                                  "reason": "Minor note logged."})
    # low urgency + no window + no customer => below silence bar
    actions = bot.layer1_evaluate(1_900_000_000.0)
    assert actions == []


# ---------------------------------------------------------------- composer
def test_fallback_grounded_no_fabricated_numbers():
    fresh_store()
    cat, merch, cust, trig = make_views()
    comp, source = bot.compose(cat, merch, trig, cust, "m01")
    assert source == "fallback"
    facts = json.dumps({"c": cat.raw, "m": merch.raw, "t": trig.raw, "cu": cust.raw})
    import re
    ctx_nums = set(re.findall(r"\d+(?:\.\d+)?", facts.replace(",", "")))
    for n in re.findall(r"\d+(?:\.\d+)?", comp["body"].replace(",", "")):
        assert n in ctx_nums, f"fabricated number {n} in fallback body"


def test_fallback_respects_taboos_and_single_cta():
    fresh_store()
    cat, merch, cust, trig = make_views()
    comp, _ = bot.compose(cat, merch, trig, cust, "m01")
    assert not cat.taboo_hits(comp["body"])
    assert not cat.taboo_hits(comp["cta"])
    assert not bot.CTA_DUP_RE.search(comp["body"])


def test_llm_composition_rejected_if_fabricates_numbers():
    fresh_store()
    cat, merch, cust, trig = make_views()
    fabricated = {"body": "Hi Bright Smile Dental — 87 patients rebooked this month already.",
                  "cta": "Shall I set it up?", "suppression_key": "x", "rationale": "r"}
    assert bot._validate_composition(fabricated, json.dumps({"m": MERCHANT, "c": CATEGORY}), cat) is None


def test_cache_prevents_second_composition_call():
    store = fresh_store()
    cat, merch, cust, trig = make_views("t_cache")
    calls = {"n": 0}
    real = bot._gemini_call

    def fake(s, u):
        calls["n"] += 1
        return {"body": "Hi Bright Smile Dental — 12 recall patients are due this week for their six-monthly check-up.",
                "cta": "Shall I set it up?", "suppression_key": "s", "rationale": "why now"}

    bot._gemini_call = fake
    bot.GEMINI_API_KEYS = ["k1"]; bot.DISABLE_LLM = False
    try:
        c1, s1 = bot.compose(cat, merch, trig, cust, "m01")
        c2, s2 = bot.compose(cat, merch, trig, cust, "m01")
    finally:
        bot._gemini_call = real
        bot.DISABLE_LLM = True
    assert calls["n"] == 1                    # quota rule: never re-call for same pair
    assert s1 == "llm" and s2 == "llm"
    assert c1["body"] == c2["body"]


# ---------------------------------------------------------------- conversations
def _state():
    store = fresh_store()
    return store.conversation("m01", merchant_id="m01")


def test_auto_reply_repeats_lead_to_wait():
    st = _state()
    r1 = respond(st, "Ok")
    r2 = respond(st, "thanks")
    assert r1["response"] == "wait" and r2["response"] == "wait"
    assert "auto_reply" in r2["reason"]


def test_hostile_ends_and_reason_recorded():
    st = _state()
    r = respond(st, "Stop messaging me. This is harassment.")
    assert r["response"] == "end" and "hostile" in r["reason"]
    assert st.phase == "ended"


def test_intent_commitment_ends_without_pushiness():
    st = _state()
    r = respond(st, "Sounds good, let's do it")
    assert r["response"] == "end" and r["reason"] == "intent_committed"
    assert st.pending_intent == "committed"


def test_genuine_interest_sends():
    st = _state()
    r = respond(st, "What are your charges for a cleaning appointment?")
    assert r["response"] == "send"


def test_end_via_reply_suppresses_future_proactive():
    store = fresh_store()
    push_category(store); push_merchant(store)
    push_trigger(store, "t_after_end")
    st = store.conversation("m01", merchant_id="m01")
    respond(st, "Please stop messaging us")
    actions = bot.layer1_evaluate(1_900_000_000.0)
    assert actions == []                    # merchant-level suppression wins


# ---------------------------------------------------------------- endpoints
def test_endpoints_conformance():
    fresh_store()
    bot.COUNTERS.update({"ticks": 0, "actions_sent": 0, "llm_calls": 0,
                         "llm_failures": 0, "fallback_used": 0, "replies": 0,
                         "contexts_accepted": 0})
    client = TestClient(bot.app)

    r = client.get("/v1/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert "loaded" in r.json() and "llm" in r.json()

    r = client.get("/v1/metadata")
    j = r.json()
    assert r.status_code == 200 and j["bot_name"] == "Vera"
    assert "team_name" in j and "model" in j

    r = client.post("/v1/context", json={"scope": "category", "context_id": "cat_dent",
                                         "version": 1, "data": CATEGORY})
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    # idempotent replay
    r2 = client.post("/v1/context", json={"scope": "category", "context_id": "cat_dent",
                                          "version": 1, "data": CATEGORY})
    assert r2.status_code == 200 and r2.json()["duplicate"] is True

    client.post("/v1/context", json={"scope": "merchant", "context_id": "m01",
                                     "version": 1, "data": MERCHANT})
    client.post("/v1/context", json={"scope": "trigger", "context_id": "t_api",
                                     "version": 1,
                                     "data": {"trigger_id": "t_api", "trigger_type": "recall_due",
                                              "reason": "12 recall patients are due for their six-monthly check-up this week.",
                                              "merchant_id": "m01", "urgency": "high"}})

    r = client.post("/v1/tick", json={"now": "2030-01-01T10:00:00Z"})
    assert r.status_code == 200 and "actions" in r.json()
    actions = r.json()["actions"]
    assert len(actions) == 1
    assert actions[0]["type"] == "send_message"
    assert actions[0]["body"] and actions[0]["cta"]

    # replay stress: same tick again -> silence
    r2 = client.post("/v1/tick", json={"now": "2030-01-01T10:05:00Z"})
    assert r2.json()["actions"] == []

    # reply flows
    r = client.post("/v1/reply", json={"merchant_id": "m01", "message": {"text": "What are your charges for cleaning?"}})
    assert r.json()["response"] == "send" and r.json()["message"]["body"]
    r = client.post("/v1/reply", json={"merchant_id": "m01", "message": {"text": "ok"}})
    assert r.json()["response"] in ("wait", "send")
    r = client.post("/v1/reply", json={"merchant_id": "m01", "message": {"text": "stop messaging me"}})
    assert r.json()["response"] == "end"

    # teardown
    r = client.post("/v1/teardown", json={})
    assert r.json()["status"] == "torn_down"
    assert client.get("/v1/healthz").json()["loaded"]["merchant"] == 0


def test_data_driven_other_vertical_no_code_change():
    """Prove the composer is category-agnostic: feed a salon CategoryContext."""
    fresh_store()
    salon = {
        "category": "salon",
        "voice": {"tone": "warm, chatty, style-forward", "style_notes": "friendly, emoji-free"},
        "taboos": ["medical claims"],
        "offer_catalog": [{"offer_id": "o1", "title": "Keratin week", "detail": "20% slot block for keratin"}],
        "peer_stats": {"rebook_rate": "38%"},
        "cta_preferences": ["Shall I block you in?"],
    }
    cat, merch, _cust, trig = make_views("t_salon")
    cat2 = CategoryView(salon)
    comp, source = bot.compose(cat2, merch, trig, None, "m09")
    assert source == "fallback"
    assert comp["body"] and comp["cta"]
    assert not cat2.taboo_hits(comp["body"] + " " + comp["cta"])
    assert "dentist" not in comp["body"].lower()   # no cross-category bleed
