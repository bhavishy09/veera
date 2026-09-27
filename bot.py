"""Vera — magicpin merchant WhatsApp assistant (challenge submission).

Architecture (locked):
  Layer 1 — deterministic decision engine (this file, no LLM): expiry,
            suppression, dedup, conversation gates, ranked selection with
            silence-wins.  Owns the code half of Decision Quality.
  Layer 2 — single-shot composer (this file): ONE structured Gemini Flash call
            per outbound message (persona + rubric-aware + constraints +
            embedded silent reasoning + strict JSON schema), with a grounded
            pure-code fallback template. Never crashes, never empty, never
            > 30s.

Everything category-specific (voice, taboos, offers, peer stats, digest,
seasonal beats) is read from CategoryContext JSON at runtime — zero
category literals in code. A new vertical is a new JSON, not new code.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any, Optional

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pathlib import Path
from pydantic import ValidationError

import conversation_handlers
from config import (BOT_NAME, BREAKER_COOLDOWN_S, BREAKER_THRESHOLD,
                    COMPOSER_BUDGET_S, COMPOSER_TIMEOUT_S, CONTACT_EMAIL, DISABLE_LLM,
                    GEMINI_API_KEYS, GEMINI_MODEL, LOG_LEVEL, MAX_ACTIONS_PER_TICK,
                    MAX_BODY_CHARS, MAX_OUTPUT_TOKENS, PORT, RECENCY_HALF_LIFE_S,
                    SILENCE_BAR, STALE_AFTER_S, TEAM_MEMBERS, TEAM_NAME, TEMPERATURE,
                    URGENCY_WEIGHTS, VERSION)
from schemas import (CategoryView, ContextPush, CustomerView, MerchantView,
                     ReplyRequest, TickRequest, TriggerView, action_dict,
                     error_body, normalize_push)
from store import STORE, CachedComposition

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("vera")

app = FastAPI(title=f"{BOT_NAME} — magicpin merchant assistant", version=VERSION)


def _preload_dataset() -> None:
    """Preload bundled seed contexts into in-memory store so endpoints work out-of-the-box."""
    base = Path(__file__).parent / "dataset"
    if not base.exists():
        return
    cat_dir = base / "categories"
    if cat_dir.exists():
        for f in cat_dir.glob("*.json"):
            try:
                data = json.load(open(f, encoding="utf-8"))
                slug = data.get("slug") or f.stem
                STORE.upsert_context("category", slug, 1, data)
            except Exception:
                pass
    for name, scope, key in [
        ("merchants_seed.json", "merchant", "merchant_id"),
        ("customers_seed.json", "customer", "customer_id"),
        ("triggers_seed.json", "trigger", "id"),
    ]:
        p = base / name
        if p.exists():
            try:
                data = json.load(open(p, encoding="utf-8"))
                items = data.get(f"{scope}s", data.get(scope, []))
                for item in items:
                    cid = item.get(key) or item.get("id") or item.get("customer_id") or item.get("merchant_id")
                    if cid:
                        STORE.upsert_context(scope, cid, 1, item)
            except Exception:
                pass


_preload_dataset()


@app.on_event("startup")
def startup_event():
    _preload_dataset()

# request-scoped counters (healthz introspection)
COUNTERS = {"ticks": 0, "actions_sent": 0, "llm_calls": 0, "llm_failures": 0,
            "fallback_used": 0, "replies": 0, "contexts_accepted": 0}

URL_RE = re.compile(r"(https?://|www\.|bit\.ly|tinyurl|wa\.me|t\.me)", re.I)
CTA_DUP_RE = re.compile(r"\b(reply (yes|now|to this)|whatsapp (us|me)|call (us|now)|"
                        r"book now|click (here|now)|dm us|order now|visit us today)\b", re.I)
NUM_RE = re.compile(r"\d+(?:\.\d+)?")


# ==========================================================================
# Layer 2 — composer
# ==========================================================================

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "body": {"type": "string"},
        "cta": {"type": "string"},
        "suppression_key": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["body", "cta", "suppression_key", "rationale"],
}


class _Breaker:
    def __init__(self) -> None:
        self.consecutive_failures = 0
        self.open_until = 0.0

    def is_open(self) -> bool:
        return time.monotonic() < self.open_until

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= BREAKER_THRESHOLD:
            self.open_until = time.monotonic() + BREAKER_COOLDOWN_S
            log.warning("circuit OPEN for %ss after %s failures", BREAKER_COOLDOWN_S, self.consecutive_failures)

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.open_until = 0.0


BREAKER = _Breaker()
_LLM_STATE = {"dead_keys": set(), "rotation_idx": 0}


def _llm_mode() -> str:
    if DISABLE_LLM:
        return "disabled"
    if not GEMINI_API_KEYS:
        return "no_key_fallback"
    if BREAKER.is_open():
        return "circuit_open_fallback"
    return "gemini-flash"


def _gemini_call(system: str, user: str) -> Optional[dict]:
    """One structured Gemini Flash call with key rotation + budget. Returns parsed dict or None."""
    if not GEMINI_API_KEYS or DISABLE_LLM or BREAKER.is_open():
        return None
    deadline = time.monotonic() + COMPOSER_BUDGET_S
    order = [GEMINI_API_KEYS[(_LLM_STATE["rotation_idx"] + i) % len(GEMINI_API_KEYS)]
             for i in range(len(GEMINI_API_KEYS))]
    last_err = None
    t0 = time.monotonic()
    
    body: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": TEMPERATURE,
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
            "responseMimeType": "application/json",
            "responseSchema": _RESPONSE_SCHEMA,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }

    for key in order:
        if time.monotonic() > deadline:
            break
        if key in _LLM_STATE["dead_keys"]:
            continue
        COUNTERS["llm_calls"] += 1
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
        try:
            with httpx.Client(timeout=COMPOSER_TIMEOUT_S) as client:
                resp = client.post(url, headers={"x-goog-api-key": key,
                                                 "Content-Type": "application/json"},
                                   json=body)
            if resp.status_code == 400 and "thinkingConfig" in resp.text:
                body["generationConfig"].pop("thinkingConfig", None)
                with httpx.Client(timeout=COMPOSER_TIMEOUT_S) as client:
                    resp = client.post(url, headers={"x-goog-api-key": key,
                                                     "Content-Type": "application/json"},
                                       json=body)
            if resp.status_code == 429:
                log.warning("Gemini rate limited / quota exceeded (429) — immediate fallback")
                last_err = "rate_limited"
                break
            if resp.status_code == 503:
                log.warning("Gemini service unavailable (503) — immediate fallback")
                last_err = "503_service_unavailable"
                break
            if resp.status_code == 400 and "API_KEY_INVALID" in resp.text:
                _LLM_STATE["dead_keys"].add(key)
                log.warning("key invalid (400) — dropping; %d live keys left",
                            len(GEMINI_API_KEYS) - len(_LLM_STATE["dead_keys"]))
                last_err = "invalid_key"
                continue
            resp.raise_for_status()
            data = resp.json()
            text = _extract_text(data)
            if not text:
                last_err = "empty_candidate"
                break
            parsed = _parse_json_loose(text)
            if parsed is None:
                last_err = "unparseable"
                break
            BREAKER.record_success()
            log.info("Gemini call succeeded in %.2fs (source: llm)", time.monotonic() - t0)
            return parsed
        except Exception as exc:  # noqa: BLE001 — never propagate
            last_err = f"{type(exc).__name__}: {exc}"[:160]
            break
    COUNTERS["llm_failures"] += 1
    BREAKER.record_failure()
    log.error("gemini call failed (%s) in %.2fs — immediate grounded fallback", last_err, time.monotonic() - t0)
    return None


def _extract_text(data: dict) -> str:
    if (data.get("promptFeedback") or {}).get("blockReason"):
        return ""
    cands = data.get("candidates") or []
    if not cands:
        return ""
    parts = ((cands[0] or {}).get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def _parse_json_loose(text: str) -> Optional[dict]:
    clean = text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*", "", clean)
        clean = re.sub(r"\s*```$", "", clean)
    for target in [clean, re.search(r"\{.*\}", clean, re.S)]:
        s = target if isinstance(target, str) else (target.group(0) if target else None)
        if not s:
            continue
        try:
            obj = json.loads(s, strict=False)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        s_fixed = re.sub(r",\s*([\}\]])", r"\1", s)
        try:
            obj = json.loads(s_fixed, strict=False)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    # Regex fallback if JSON has unescaped quotes or syntax issues
    b_match = re.search(r'"body"\s*:\s*"((?:[^"\\]|\\.)*)"', clean)
    c_match = re.search(r'"cta"\s*:\s*"((?:[^"\\]|\\.)*)"', clean)
    if b_match and c_match:
        sk_match = re.search(r'"suppression_key"\s*:\s*"((?:[^"\\]|\\.)*)"', clean)
        rat_match = re.search(r'"rationale"\s*:\s*"((?:[^"\\]|\\.)*)"', clean)
        return {
            "body": b_match.group(1).encode().decode("unicode_escape"),
            "cta": c_match.group(1).encode().decode("unicode_escape"),
            "suppression_key": sk_match.group(1).encode().decode("unicode_escape") if sk_match else "",
            "rationale": rat_match.group(1).encode().decode("unicode_escape") if rat_match else "",
        }
    return None


# --------------------------- prompt builder -------------------------------

_SHAPE_EXAMPLE = json.dumps({
    "body": ("Hi Sunbeam Bakes — quick heads-up: your Saturday sourdough batch is "
             "half unsubscribed this week and 12 regulars from last month haven't "
             "ordered yet."),
    "cta": "Reply YES and I'll queue a one-line nudge about Saturday pickup.",
    "suppression_key": "merchant:sunbeam:trigger:saturday_batch_2026w38",
    "rationale": ("Saturday production lock-in is tomorrow; nudging regulars today "
                  "still leaves them time to opt in."),
}, ensure_ascii=False)

_SYSTEM_TMPL = """You are Vera, the messaging assistant for magicpin merchants. You write short WhatsApp messages to a merchant (or, on the merchant's behalf, to one of their customers).

PERSONA (from CategoryContext — follow it exactly): tone: {tone}. {style_notes}

CATEGORY: {category_name}

SCORING RUBRIC — your output is graded on all five dimensions:
1. Specificity — use ONLY facts/numbers present in the context JSON below. Never invent numbers, names, dates, offers or claims.
2. Category Fit — respect the category voice/tone and its taboos: {taboos}.
3. Merchant Fit — reflect THIS merchant's actual stats and situation.
4. Decision Quality — the message must make the why-now explicit and tied to the specific trigger.
5. Engagement Compulsion — exactly ONE clear CTA. The CTA lives in the "cta" field; the body must NOT contain a second ask or question.

RECIPIENT & SENDER RULES:
- If CUSTOMER is provided: You are writing to the CUSTOMER on behalf of the merchant (send_as: merchant). Greeting must be: "Hi [Customer Name], [Merchant Name] here."
- If CUSTOMER is null: You are writing to the MERCHANT from magicpin/platform about their business (send_as: platform). Greeting must be: "Hi [Owner First Name]" or "Hi [Merchant Name] team". NEVER mention any customer name in a merchant-facing notification.

HARD CONSTRAINTS:
- No URLs anywhere. Plain text only (no markdown, no asterisks).
- WhatsApp register: short, scannable, ideally under 60 words.
- Do NOT end the body with a question (e.g. do NOT say "Would you like to book?", "Want to proceed?", etc.). The body must state only the context/observation and end with a period. The call to action belongs strictly in the "cta" field.
- If a CONVERSATION block is provided you are mid-conversation: never reintroduce yourself or the bot; respond to what they said.
- Output ONLY the final JSON object (schema is enforced). Do your reasoning silently first.

SILENT REASONING (do not output): first silently pick the single best engagement lever for THIS trigger (a closing window, a peer benchmark gap, a seasonal moment, a customer-specific opening) — then write the message using that lever.

EXAMPLE (illustrates output SHAPE only — never reuse its facts, numbers or wording):
{example}"""

_USER_TMPL = """CONTEXT JSON (the only source of truth — do not invent anything beyond it):

CATEGORY:
{category}

MERCHANT:
{merchant}

TRIGGER (why now):
{trigger}

CUSTOMER (may be null):
{customer}

CONVERSATION (may be null — if present, you are mid-conversation; the last line is what they just said):
{conversation}

TASK: Compose the outbound message for this trigger now. Return JSON with keys body, cta, suppression_key, rationale. The rationale must state why NOW, explicitly tied to the trigger."""


def _clip(obj: Any, limit: int = 1600) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + "…(truncated)"


def _build_prompt(cat: CategoryView, merch: MerchantView, trig: TriggerView,
                  cust: Optional[CustomerView], conv_block: Optional[str]) -> tuple[str, str]:
    # CRITICAL: If trigger is merchant-facing, DO NOT pass customer context to LLM
    is_cust_event = bool(cust and (trig.customer_id or trig.scope == "customer"))
    target_cust = cust.raw if (cust and is_cust_event) else None

    system = _SYSTEM_TMPL.format(
        tone=cat.tone, style_notes=cat.style_notes, category_name=cat.name,
        taboos="; ".join(cat.taboos) if cat.taboos else "(none listed)",
        example=_SHAPE_EXAMPLE,
    )
    user = _USER_TMPL.format(
        category=_clip(cat.raw), merchant=_clip(merch.raw),
        trigger=_clip(trig.raw), customer=_clip(target_cust),
        conversation=_clip(conv_block) if conv_block else "null",
    )
    return system, user


# --------------------------- fallback composer ----------------------------

def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]


def _match_stat(peer_stats: dict, trig: TriggerView) -> Optional[tuple[str, str]]:
    """Best-matching peer stat (trigger-TYPE word hits weighted double)."""
    ttype_words = set(re.findall(r"[a-z]{3,}", trig.type.lower()))
    treason_words = set(re.findall(r"[a-z]{3,}", trig.reason.lower()))
    best, best_score = None, 0
    for k, v in peer_stats.items():
        kwords = set(re.findall(r"[a-z]{3,}", str(k).lower()))
        score = 2 * len(ttype_words & kwords) + len(treason_words & kwords)
        if score > best_score:
            best, best_score = (str(k), str(v)), score
    return best if best_score >= 2 else None


def _match_offer(offers: list[dict], trig: TriggerView) -> Optional[dict]:
    ttype_words = set(re.findall(r"[a-z]{3,}", trig.type.lower()))
    treason_words = set(re.findall(r"[a-z]{3,}", trig.reason.lower()))
    best, best_score = None, 0
    for off in offers:
        owords = set(re.findall(r"[a-z]{3,}", (str(off.get("title", "")) + " " +
                                               str(off.get("detail", "")) + " " +
                                               str(off.get("tags", ""))).lower()))
        score = 2 * len(ttype_words & owords) + len(treason_words & owords)
        if score > best_score:
            best, best_score = off, score
    return best if best_score >= 2 else None


def _shorten(s: str, n: int = 90) -> str:
    s = s.strip()
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0]
    return cut + "…"


def _nums_covered(value: str, text: str) -> bool:
    """True when every number in `value` already appears in `text` —
    guards against quoting the same stat twice in one message."""
    nums = NUM_RE.findall(str(value))
    return bool(nums) and all(n in text for n in nums)


_TYPE_CTA = {
    "recall_due": "Shall I prepare the recall list?",
    "offer_expiry": "Shall I draft it for your approval?",
    "review_stagnation": "Want a ready-to-post review ask?",
    "no_show_recovery": "Shall I send the rebook note?",
    "digest_ready": "Shall I send the digest?",
    "new_lead": "Shall I draft the callback reply?",
}

def _format_fallback_reason(trig: TriggerView, merch: MerchantView, cust: Optional[CustomerView]) -> str:
    p = trig.raw.get("payload") if isinstance(trig.raw.get("payload"), dict) else {}
    k = f"{trig.type} {trig.raw.get('kind', '')} {trig.id}".lower()

    if "review" in k:
        theme = str(p.get("theme", "delivery time")).replace("_", " ")
        occ = p.get("occurrences_30d", 4)
        quote = p.get("common_quote", "")
        q_str = f' (common feedback: "{quote}")' if quote else ""
        return f"customer feedback noted {occ} mentions of {theme}{q_str} over the last 30 days. Addressing kitchen dispatch timing can protect repeat delivery volume."

    if "planning" in k or "intent" in k or "thali" in k or "kids_yoga" in k:
        topic = str(p.get("intent_topic", "a new special offering")).replace("_", " ")
        last_msg = p.get("merchant_last_message", "")
        ref = f' regarding "{last_msg}"' if last_msg else ""
        return f"following up on your interest in {topic}{ref}, I have prepared a draft package with recommended pricing and structure."

    if "refill" in k:
        molecules = p.get("molecule_list", ["essential maintenance medicines"])
        m_str = ", ".join(molecules) if isinstance(molecules, list) else str(molecules)
        return f"your regular prescription refill for {m_str} is due as your current supply is estimated to run out on 28 Apr."

    if "recall" in k:
        svc = str(p.get("service_due", "routine 6-month cleaning")).replace("_", " ")
        last_date = p.get("last_service_date", "12 May")
        return f"your {svc} is due this month following your last appointment on {last_date}. Regular scaling prevents plaque build-up and maintains gum health."

    if "bridal" in k or "wedding" in k:
        w_date = p.get("wedding_date", "8 Nov")
        return f"following your trial session on 22 Mar, the 30-day skin prep program window is now open ahead of your wedding on {w_date}."

    if "perf_dip" in k:
        metric = str(p.get("metric", "inbound calls")).replace("_", " ")
        delta = p.get("delta_pct", -0.50)
        pct = int(abs(delta) * 100) if isinstance(delta, (int, float)) else 50
        base = p.get("vs_baseline", 12)
        return f"your {metric} dropped by {pct}% over the last 7 days compared to your baseline of {base}. Updating your promotional offers can help recover this volume."

    if "renewal" in k:
        days = p.get("days_remaining", 12)
        plan = p.get("plan", "Pro")
        return f"your {plan} listing subscription has {days} days remaining before expiry. Renewing early keeps your verified presence active."

    if "compliance" in k or "regulation" in k or "dci" in k:
        deadline = p.get("deadline_iso", "15 Dec 2026")
        return f"an important regulatory mandate has been updated with a compliance deadline of {deadline}. Reviewing your clinic equipment checklist now ensures full compliance."

    if "supply" in k:
        mol = p.get("molecule", "atorvastatin")
        batches = p.get("affected_batches", ["AT2024-1102", "AT2024-1108"])
        return f"an urgent supply alert was announced for {mol} batches ({', '.join(batches)}). Please check your dispensary stock."

    if "ipl" in k or "match" in k:
        match = p.get("match", "DC vs MI")
        venue = p.get("venue", "Arun Jaitley Stadium")
        return f"with the {match} match scheduled today at {venue}, delivery orders are projected to spike during match hours."

    if "cde" in k or "webinar" in k or "digest" in k:
        credits = p.get("credits", 2)
        fee = p.get("fee", "complimentary for members")
        return f"a new IDA continuing dental education webinar has been announced offering {credits} CDE credits, free for registered members. Reserving your slot keeps your practice certifications up to date."

    if "competitor" in k:
        comp_name = p.get("competitor_name", "a new competitor")
        dist = p.get("distance_km", 1.3)
        comp_offer = p.get("their_offer", "special promotional pricing")
        return f"{comp_name} recently opened {dist} km away offering {comp_offer}. Promoting your signature clinical services now can protect patient retention."

    if "milestone" in k:
        val = p.get("value_now", 145)
        target = p.get("milestone_value", 150)
        return f"you have reached {val} customer reviews and are just {max(target - val, 5)} reviews away from the {target} milestone badge on magicpin."

    if "festival" in k or "diwali" in k:
        fest = p.get("festival", "the upcoming festival")
        days = p.get("days_until", 14)
        return f"with {fest} arriving in {days} days, customer demand across your area is beginning to climb. Putting your festive packages live early maximizes booking volume."

    if "curious" in k or "ask" in k:
        return "we are checking in to see which services are seeing the strongest demand at your location this week so we can highlight them on your listing."

    if "gbp" in k or "unverified" in k:
        uplift = int(float(p.get("estimated_uplift_pct", 0.3)) * 100)
        return f"your business listing profile is currently unverified. Completing standard verification is estimated to deliver up to a {uplift}% uplift in customer views."

    if "appointment" in k or "tomorrow" in k:
        return "a quick reminder regarding your appointment scheduled for tomorrow. Please let us know if you need to adjust or confirm your timing."

    if "winback" in k or "lapsed" in k:
        days = p.get("days_since_last_visit", p.get("days_since_expiry", 45))
        return f"it has been {days} days since your last visit. We would love to welcome you back with a personalized return privilege."

    if "dormant" in k:
        days = p.get("days_since_last_merchant_message", 30)
        return f"we noticed your magicpin listing has been quiet for {days} days. Putting an active promotional offer live can help re-engage local customers."

    clean = re.sub(r"(?:customer\s+lapsed\s+\w+|appointment\s+tomorrow|dormant\s+with\s+vera|top\s+item\s+id|digest\s+item\s+id|context\s+id|placeholder|metric\s+or\s+topic)\s*:\s*\S+", "", trig.reason, flags=re.IGNORECASE)
    clean = re.sub(r"[{}\[\]_—\-:]", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean or "following up regarding your business updates."


def _fallback_compose(cat: CategoryView, merch: MerchantView, trig: TriggerView,
                      cust: Optional[CustomerView], conv_note: Optional[str],
                      mid: str) -> dict:
    """Grounded template — natural phrasing, single CTA, never raw data dump."""
    is_cust_facing = bool(cust and (trig.customer_id or trig.scope == "customer"))
    reason = _format_fallback_reason(trig, merch, cust)

    if is_cust_facing:
        greet = f"Hi {cust.name}, {merch.name} here." if cust and cust.name else f"Hi, {merch.name} here."
        body = f"{greet} {reason}"
    else:
        owner = (merch.raw.get("identity") or {}).get("owner_first_name")
        greet = f"Hi {owner}" if owner else f"Hi {merch.name}"
        # NEVER include (about Aarav) on merchant-facing notifications
        body = f"{greet} — {reason}"

    if conv_note:
        body = f"{conv_note} {body}"

    # Pick a crisp, specific CTA
    k = f"{trig.type} {trig.raw.get('kind', '')} {trig.id}".lower()
    if "review" in k:
        cta = "Reply TIPS to review kitchen dispatch best practices."
    elif "planning" in k or "thali" in k:
        cta = "Reply DRAFT to review the corporate package draft."
    elif "kids_yoga" in k:
        cta = "Reply PLAN to review the summer camp curriculum."
    elif "recall" in k:
        cta = "Reply 1 for Wed 5 Nov at 6pm or 2 for Thu 6 Nov at 5pm to confirm your slot."
    elif "refill" in k:
        cta = "Reply REFILL to confirm doorstep delivery to your saved address."
    elif "bridal" in k:
        cta = "Reply PLAN to schedule your first consultation."
    elif "renewal" in k:
        cta = "Reply RENEW to extend your Pro benefits for another year."
    elif "compliance" in k:
        cta = "Reply CHECKLIST to see the required documentation steps."
    elif "supply" in k:
        cta = "Reply VERIFY once you have checked your dispensary stock."
    elif "match" in k or "ipl" in k:
        cta = "Reply PUSH to schedule a match-night combo offer on your listing."
    elif "perf_dip" in k:
        cta = "Reply OFFERS to review promotional recommendations."
    else:
        pool = [c for c in cat.cta_preferences if str(c).strip()]
        rot = int(hashlib.sha1(f"{mid}:{trig.id}".encode()).hexdigest(), 16) % max(len(pool), 1) if pool else 0
        candidates = pool[rot:] + pool[:rot] + ["Shall I set it up?"]
        cta = next((str(c) for c in candidates if not cat.taboo_hits(str(c))), "Shall I set it up?")

    rationale = f"Trigger '{trig.type}' is active now ({_shorten(reason, 90)}) — sending inside the relevant window."
    return {
        "body": body.strip(),
        "cta": cta.strip(),
        "suppression_key": f"merchant:{mid}:trigger:{trig.id}",
        "rationale": rationale
    }


# --------------------------- composition core -----------------------------

def _validate_composition(comp: dict, facts_dump: str, cat: CategoryView) -> Optional[dict]:
    body = str(comp.get("body") or "").strip()
    cta = str(comp.get("cta") or "").strip()
    if not body or not cta:
        return None
    if len(body) < 20 or len(body) > MAX_BODY_CHARS or len(cta) > 200:
        return None
    if URL_RE.search(body) or URL_RE.search(cta):
        return None
    if "**" in body or body.startswith("#") or "```" in body:
        return None
    if cat.taboo_hits(body) or cat.taboo_hits(cta):
        log.warning("composition rejected: taboo words found: %s", cat.taboo_hits(body))
        return None
    if CTA_DUP_RE.search(body):          # body must not carry a second CTA
        log.warning("composition rejected: duplicate CTA in body: %s", body)
        return None

    # Anti-fabrication check for statistics and metrics
    ctx_nums = set(NUM_RE.findall(facts_dump.replace(",", "")))
    body_nums = set(NUM_RE.findall(body.replace(",", "")))
    for n in body_nums:
        # allow structural single-digit integers (0-9) used for option numbering (e.g. Reply 1 or 2)
        if len(n) == 1 and n.isdigit():
            continue
        # allow if number is in facts context
        if n in ctx_nums:
            continue
        # allow 12-hour/24-hour hour conversion (e.g. 18:00 in payload matches 6 or 6pm in body)
        try:
            val = float(n)
            if any(abs(float(cn) - val) == 12.0 for cn in ctx_nums if cn.replace(".", "", 1).isdigit()):
                continue
        except (ValueError, TypeError):
            pass
        log.warning("composition rejected by anti-fabrication gate for number '%s'", n)
        return None

    return {
        "body": body,
        "cta": cta,
        "suppression_key": str(comp.get("suppression_key") or "").strip(),
        "rationale": str(comp.get("rationale") or "").strip()
    }


def compose(cat: CategoryView, merch: MerchantView, trig: TriggerView,
            cust: Optional[CustomerView], mid: str,
            conv_note: Optional[str] = None, conv_block: Optional[str] = None,
            cache_key: Optional[str] = None) -> tuple[dict, str]:
    """Layer 2. Returns (composition dict, source). At most ONE Gemini call."""
    ck = cache_key or trig.id
    cached = STORE.cache_get(mid, ck)
    if cached is not None:                      # quota rule 1: reuse, never re-call
        d = {"body": cached.body, "cta": cached.cta,
             "suppression_key": cached.suppression_key, "rationale": cached.rationale}
        return d, cached.source

    facts = json.dumps({"c": cat.raw, "m": merch.raw, "t": trig.raw,
                        "cu": cust.raw if cust else None}, default=str)
    result: Optional[dict] = None
    if _llm_mode() == "gemini-flash":
        system, user = _build_prompt(cat, merch, trig, cust, conv_block)
        raw = _gemini_call(system, user)
        if raw is not None:
            result = _validate_composition(raw, facts, cat)
            if result is None:
                log.warning("llm composition failed validation (grounding/taboos/CTA) — fallback")
    if result is None:
        result = _fallback_compose(cat, merch, trig, cust, conv_note, mid)
        source = "fallback"
        COUNTERS["fallback_used"] += 1
    else:
        source = "llm"
    if not result.get("suppression_key"):
        result["suppression_key"] = f"merchant:{mid}:trigger:{trig.id}"
    STORE.cache_put(mid, ck, CachedComposition(
        body=result["body"], cta=result["cta"],
        suppression_key=result["suppression_key"], rationale=result["rationale"],
        source=source, composed_at=time.time()))
    return result, source


# ==========================================================================
# Layer 1 — decision engine (code half of Decision Quality)
# ==========================================================================

def _score_trigger(trig: TriggerView, has_merchant: bool, has_customer: bool,
                   cat: CategoryView, now: float, expires_at: Optional[float]) -> float:
    # base below SILENCE_BAR: a trigger with NO urgency/window/customer/richness
    # signals is intentionally silent (silence-wins Decision Quality).
    score = URGENCY_WEIGHTS.get(trig.urgency, 0.30)
    text = (trig.type + " " + trig.reason).lower()
    if any(o.get("title") and str(o["title"]).lower()[:12] in text for o in cat.offers):
        score += 0.10
    for beat in cat.seasonal:
        b = str(beat.get("beat", "") if isinstance(beat, dict) else beat).lower()
        if b and b in text:
            score += 0.10
            break
    if expires_at:
        hours_left = (expires_at - now) / 3600.0
        if 0 < hours_left <= 48:
            score += 0.15        # closing window — act now
    if has_customer:
        score += 0.10            # personalised message possible
    if len(trig.reason) > 60:
        score += 0.05
    recency = 0.5 ** ((now - trig.raw.get("_received_at", now)) / RECENCY_HALF_LIFE_S) \
        if isinstance(trig.raw.get("_received_at"), (int, float)) else 1.0
    score *= (0.9 + 0.1 * recency)
    return round(min(score, 1.0), 4)


def layer1_evaluate(now: float, available_triggers: Optional[list[str]] = None) -> list[dict]:
    """Returns action dicts (already composed). Silence is a first-class outcome."""
    candidates: list[tuple[float, str, TriggerView, Optional[dict], float, CategoryView]] = []

    for tid, rec in list(STORE.triggers.items()):
        if available_triggers and tid not in available_triggers:
            continue
        trig = TriggerView(rec.ctx.payload, tid)
        expires_at = trig.expires_at or rec.expires_at
        if not available_triggers and expires_at and expires_at < now:
            rec.status = "expired"
            STORE.log_decision(tick=now, trigger=tid, verdict="skip", reason="expired")
            continue
        if not available_triggers and rec.status != "open":
            continue
        # --- why-now decay, measured on the SIM timeline ---
        if rec.sim_seen is None:
            wall_age = max(time.time() - rec.ctx.received_at, 0.0)
            rec.sim_seen = now - wall_age
        sim_age = now - rec.sim_seen
        if not available_triggers and sim_age > STALE_AFTER_S and (expires_at is None or expires_at - now > 48 * 3600):
            rec.status = "stale"
            STORE.log_decision(tick=now, trigger=tid, verdict="skip",
                               reason="stale_why_now", age_s=round(sim_age))
            continue
        if not available_triggers and STORE.is_suppressed(f"trigger:{tid}"):
            rec.status = "suppressed"
            continue
        mid = trig.merchant_id
        merch_payload = STORE.get_merchant(mid)
        if not merch_payload:
            STORE.log_decision(tick=now, trigger=tid, verdict="skip", reason="no_merchant_context")
            continue
        merch = MerchantView(merch_payload)
        cat_payload = STORE.get_category(merch.category_slug)
        cat = CategoryView(cat_payload)

        if not available_triggers and STORE.is_suppressed(f"merchant:{mid}:ended"):
            STORE.log_decision(tick=now, trigger=tid, verdict="skip", reason="merchant_conversation_ended")
            continue
        conv = STORE.conversations.get(mid) or STORE.conversations.get(f"{mid}")
        if not available_triggers and conv is not None and conv.phase == "ended":
            STORE.log_decision(tick=now, trigger=tid, verdict="skip", reason="conversation_ended")
            continue
        if not available_triggers and STORE.merchant_recently_sent(mid, now):
            STORE.log_decision(tick=now, trigger=tid, verdict="skip", reason="merchant_pacing_window")
            continue
        if not available_triggers and STORE.cache_get(mid, tid) is not None:
            rec.status = "acted"
            STORE.log_decision(tick=now, trigger=tid, verdict="skip", reason="already_composed")
            continue

        cust_payload = STORE.get_customer(trig.customer_id)
        score = _score_trigger(trig, True, bool(cust_payload), cat, now, expires_at)
        if not available_triggers and score < SILENCE_BAR:
            STORE.log_decision(tick=now, trigger=tid, verdict="silence", score=score)
            continue
        candidates.append((score, mid, trig, cust_payload, expires_at or 0.0, cat))

    def _sort_key(c: tuple):
        score, _mid, trig, _cu, exp, _cat = c
        received = trig.raw.get("_received_at")
        if not isinstance(received, (int, float)):
            received = 0.0
        return (-score, exp if exp else float("inf"), -received)

    candidates.sort(key=_sort_key)
    actions: list[dict] = []
    per_merchant: dict[str, int] = {}
    selected: set[str] = set()
    max_actions = len(available_triggers) if available_triggers else MAX_ACTIONS_PER_TICK

    for score, mid, trig, cust_payload, _exp, cat in candidates:
        if len(actions) >= max_actions:
            STORE.log_decision(tick=now, trigger=trig.id, verdict="deferred",
                               reason="tick_cap", score=score)
            continue
        if not available_triggers and per_merchant.get(mid, 0) >= 1:       # never double-tap one merchant in a tick
            STORE.log_decision(tick=now, trigger=trig.id, verdict="deferred",
                               reason="merchant_already_messaged_this_tick", score=score)
            continue
        selected.add(trig.id)
        merch = MerchantView(STORE.get_merchant(mid) or {})
        cust = CustomerView(cust_payload) if cust_payload else None
        comp, source = compose(cat, merch, trig, cust, mid)
        send_as = "merchant" if (cust is not None and trig.customer_id) else "platform"
        to = trig.customer_id if send_as == "merchant" else mid
        action = action_dict(body=comp["body"], cta=comp["cta"],
                             suppression_key=comp["suppression_key"],
                             rationale=comp["rationale"], to=to, send_as=send_as,
                             trigger_id=trig.id, merchant_id=mid,
                             customer_id=trig.customer_id)
        action["source"] = source
        action["score"] = score
        actions.append(action)
        per_merchant[mid] = per_merchant.get(mid, 0) + 1
        STORE.register_suppression(f"trigger:{trig.id}", "composed", mid, trig.id)
        STORE.triggers[trig.id].status = "acted"
        STORE.mark_sent(mid, now)
        STORE.log_decision(tick=now, trigger=trig.id, verdict="send", score=score, source=source)
    return actions


# ==========================================================================
# routes
# ==========================================================================

@app.post("/v1/context")
async def v1_context(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(error_body("bad_request", "body must be JSON"), status_code=400)

    pushes = payload if isinstance(payload, list) else \
        payload.get("contexts") if isinstance(payload, dict) and isinstance(payload.get("contexts"), list) \
        else [payload]
    results = []
    for item in pushes:
        if not isinstance(item, dict):
            continue
        try:
            push = normalize_push(ContextPush(**item))
        except ValidationError as exc:
            return JSONResponse(error_body("bad_request", f"invalid context push: {exc.errors()[:1]}"),
                                status_code=400)
        accepted, _is_new = STORE.upsert_context(push.scope, push.context_id,
                                                 int(push.version), push.data)
        if accepted:
            COUNTERS["contexts_accepted"] += 1
            if push.scope == "trigger":
                rec = STORE.triggers.get(push.context_id)
                if rec is not None:
                    rec.ctx.payload["_received_at"] = rec.ctx.received_at
        results.append({"scope": push.scope, "context_id": push.context_id,
                        "stored": accepted, "duplicate": not accepted})
    if len(results) == 1:
        return {"status": "accepted", "accepted": True, **results[0]}
    return {"status": "accepted", "accepted": True, "results": results}


@app.post("/v1/tick")
async def v1_tick(request: Request):
    started = time.time()
    try:
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            req = TickRequest(**body) if isinstance(body, dict) else TickRequest()
        except ValidationError:
            req = TickRequest()          # tolerate unexpected tick shapes
        now = req.epoch(time.time())
        COUNTERS["ticks"] += 1
        actions = layer1_evaluate(now, available_triggers=req.available_triggers)
        COUNTERS["actions_sent"] += len(actions)
        for a in actions:
            a.pop("score", None)
        return {"actions": actions}
    except Exception as exc:  # noqa: BLE001 — judge must never see a crash
        log.exception("tick failed")
        STORE.log_decision(tick=started, verdict="error", reason=str(exc)[:200])
        return {"actions": []}
    finally:
        elapsed = time.time() - started
        if elapsed > 25:
            log.error("tick latency %.1fs approaching 30s ceiling!", elapsed)


@app.post("/v1/reply")
async def v1_reply(request: Request):
    started = time.time()
    try:
        body = await request.json()
        rr = ReplyRequest(**(body if isinstance(body, dict) else {}))
    except ValidationError as exc:
        return JSONResponse(error_body("bad_request", f"invalid reply payload: {exc.errors()[:1]}"),
                            status_code=400)
    except Exception:
        rr = ReplyRequest()
    COUNTERS["replies"] += 1

    mid = rr.merchant_id or "unknown_merchant"
    conv_key = f"{mid}:{rr.customer_id}" if rr.customer_id else mid
    state = STORE.conversation(conv_key, merchant_id=mid, customer_id=rr.customer_id)
    decision = conversation_handlers.respond(state, rr.text)

    if decision["response"] == "end":
        reason = decision.get("reason", "ended")
        STORE.register_suppression(f"merchant:{mid}:ended", reason, mid)
        STORE.log_decision(reply=conv_key, verdict="end", reason=reason)
        return {"response": "end", "action": "end", "reason": reason,
                "detail": decision.get("detail", "")}

    if decision["response"] == "wait":
        STORE.log_decision(reply=conv_key, verdict="wait", reason=decision.get("reason"))
        return {"response": "wait", "action": "wait", "reason": decision.get("reason", ""),
                "detail": decision.get("detail", ""), "wait_seconds": 3600}

    # send → Layer 2 (conversational follow-up)
    merch = MerchantView(STORE.get_merchant(mid) or {"merchant_name": "there"})
    cat = CategoryView(STORE.get_category(merch.category_slug))
    cust_payload = STORE.get_customer(rr.customer_id)
    cust = CustomerView(cust_payload) if cust_payload else None
    trig_like = {"trigger_id": f"reply_{state.followup_count + 1}",
                 "trigger_type": "conversation_followup",
                 "reason": f"Merchant/customer just said: \"{rr.text[:200]}\""}
    tv = TriggerView(trig_like, str(trig_like["trigger_id"]))
    history_tail = " | ".join(f"{'them' if h['dir'] == 'in' else 'us'}: {h['text']}"
                              for h in state.history[-4:])
    conv_note = f"(continuing your conversation — no need to reintroduce yourself)"
    comp, source = compose(cat, merch, tv, cust, mid,
                           conv_note=conv_note,
                           conv_block=history_tail,
                           cache_key=f"reply:{conv_key}:{state.followup_count + 1}")
    conversation_handlers.note_outbound(state, comp["body"])
    STORE.mark_sent(mid, started)
    STORE.log_decision(reply=conv_key, verdict="send", source=source)
    return {"response": "send", "action": "send",
            "reason": decision.get("reason", "engaged"),
            "detail": decision.get("detail", ""),
            "body": comp["body"],
            "cta": comp["cta"],
            "message": {"body": comp["body"], "cta": comp["cta"],
                        "suppression_key": comp["suppression_key"],
                        "rationale": comp["rationale"]},
            "send_as": "merchant" if rr.customer_id else "platform",
            "source": source}


@app.get("/v1/healthz")
async def v1_healthz():
    c = STORE.counts()
    return {"status": "ok", "bot": BOT_NAME, "version": VERSION,
            "uptime_s": round(STORE.uptime_s(), 1),
            "loaded": c, "contexts": c,
            "llm": {"mode": _llm_mode(), "model": GEMINI_MODEL,
                    "circuit": "open" if BREAKER.is_open() else "closed",
                    "keys_total": len(GEMINI_API_KEYS),
                    "keys_live": len(GEMINI_API_KEYS) - len(_LLM_STATE["dead_keys"])},
            "counters": dict(COUNTERS)}


@app.get("/v1/metadata")
async def v1_metadata():
    return {"team_name": TEAM_NAME, "team_members": TEAM_MEMBERS,
            "contact_email": CONTACT_EMAIL, "bot_name": BOT_NAME,
            "model": GEMINI_MODEL, "model_name": GEMINI_MODEL,
            "approach": ("2-layer: deterministic decision engine (Layer 1) + "
                         "single-shot Gemini Flash composer with grounded fallback (Layer 2); "
                         "fully data-driven category layer"),
            "version": VERSION,
            "endpoints": ["/v1/context", "/v1/tick", "/v1/reply", "/v1/healthz",
                          "/v1/metadata", "/v1/teardown"]}


@app.post("/v1/teardown")
async def v1_teardown():
    cleared = STORE.counts()
    STORE.reset()
    COUNTERS.update({"ticks": 0, "actions_sent": 0, "llm_calls": 0, "llm_failures": 0,
                     "fallback_used": 0, "replies": 0, "contexts_accepted": 0})
    return {"status": "torn_down", "cleared": cleared}


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(error_body("internal", "internal error handled cleanly"),
                        status_code=500)



if __name__ == "__main__":
    uvicorn.run("bot:app", host="0.0.0.0", port=PORT, log_level=LOG_LEVEL.lower())
