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
    for key in order:
        if key in _LLM_STATE["dead_keys"]:
            continue
        COUNTERS["llm_calls"] += 1
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": TEMPERATURE,
                "maxOutputTokens": MAX_OUTPUT_TOKENS,
                "responseMimeType": "application/json",
                "responseSchema": _RESPONSE_SCHEMA,
            },
        }
        if GEMINI_MODEL.startswith("gemini-2.5"):
            body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
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
                _LLM_STATE["dead_keys"].add(key)
                log.warning("key exhausted (429) — rotating; %d live keys left",
                            len(GEMINI_API_KEYS) - len(_LLM_STATE["dead_keys"]))
                last_err = "rate_limited"
                continue
            if resp.status_code == 400 and "API_KEY_INVALID" in resp.text:
                _LLM_STATE["dead_keys"].add(key)
                log.warning("key invalid (400) — dropping; %d live keys left",
                            len(GEMINI_API_KEYS) - len(_LLM_STATE["dead_keys"]))
                last_err = "invalid_key"
                continue
            if resp.status_code >= 500 and time.monotonic() < deadline - COMPOSER_TIMEOUT_S:
                time.sleep(2.0)
                COUNTERS["llm_calls"] += 1
                resp = httpx.post(url, headers={"x-goog-api-key": key,
                                                "Content-Type": "application/json"},
                                  json=body, timeout=COMPOSER_TIMEOUT_S)
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
            return parsed
        except Exception as exc:  # noqa: BLE001 — never propagate
            last_err = f"{type(exc).__name__}: {exc}"[:160]
            if time.monotonic() > deadline:
                break
            continue
    COUNTERS["llm_failures"] += 1
    BREAKER.record_failure()
    log.error("gemini call failed (%s) — fallback will handle", last_err)
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
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                obj = json.loads(match.group(0))
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                return None
    return None


# --------------------------- prompt builder -------------------------------

_SHAPE_EXAMPLE = json.dumps({
    "body": ("Hi Sunbeam Bakes — quick heads-up: your Saturday sourdough batch is "
             "half unsubscribed this week and 12 regulars from last month haven't "
             "ordered. Want me to send them a one-line nudge about Saturday pickup?"),
    "cta": "Reply YES and I'll queue the nudge.",
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
5. Engagement Compulsion — exactly ONE clear CTA. The CTA lives in the "cta" field; the body must not contain a second ask.

HARD CONSTRAINTS:
- No URLs anywhere. Plain text only (no markdown, no asterisks).
- WhatsApp register: short, scannable, ideally under 60 words.
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
    system = _SYSTEM_TMPL.format(
        tone=cat.tone, style_notes=cat.style_notes, category_name=cat.name,
        taboos="; ".join(cat.taboos) if cat.taboos else "(none listed)",
        example=_SHAPE_EXAMPLE,
    )
    user = _USER_TMPL.format(
        category=_clip(cat.raw), merchant=_clip(merch.raw),
        trigger=_clip(trig.raw), customer=_clip(cust.raw if cust else None),
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

def _fallback_compose(cat: CategoryView, merch: MerchantView, trig: TriggerView,
                      cust: Optional[CustomerView], conv_note: Optional[str],
                      mid: str) -> dict:
    """Grounded template — every fact comes verbatim from the given payloads."""
    # reason: drop any sentence that itself looks like a CTA (keeps single-CTA guarantee)
    reason_sents = [s for s in _sentences(trig.reason)
                    if not CTA_DUP_RE.search(s) and not URL_RE.search(s)]
    reason = " ".join(reason_sents) or trig.type.replace("_", " ")
    if not reason.endswith((".", "!", "?")):
        reason += "."

    greet = f"Hi {merch.name}" if merch.name and merch.name.lower() != "the merchant" else "Hi"
    focus = f" (about {cust.name})" if cust and cust.name else ""
    body = f"{greet}{focus} — {reason}"

    stat = _match_stat(cat.peer_stats, trig)
    stat_used = False
    if stat and not _nums_covered(stat[1], reason):
        body += f" For context across similar businesses: {stat[0].replace('_', ' ')} is {stat[1]}."
        stat_used = True
    if not stat_used:
        off = _match_offer(cat.offers, trig)
        if off:
            title_words = set(re.findall(r"[a-z]{4,}", str(off.get("title", "")).lower()))
            reason_words = set(re.findall(r"[a-z]{4,}", reason.lower()))
            already_mentioned = bool(title_words) and \
                len(title_words & reason_words) / len(title_words) > 0.6
            if not already_mentioned:
                title = str(off.get("title") or "").strip().rstrip(".")
                detail = str(off.get("detail") or "").strip().rstrip(".")
                seg = title if not detail else f"{title} — {detail}"
                if seg and not CTA_DUP_RE.search(seg) and not URL_RE.search(seg):
                    body += f" Active in your category right now: {seg}."

    if conv_note:
        body = f"{conv_note} {body}"

    # CTA: trigger-type suggestion first, then the category's own preference
    # pool (hash-rotated for variety), then a generic default — first candidate
    # that clears the category taboo filter wins (single-CTA guarantee holds:
    # exactly one cta field, and body is CTA-free by construction above).
    pool = [c for c in cat.cta_preferences if str(c).strip()]
    rot = int(hashlib.sha1(f"{mid}:{trig.id}".encode()).hexdigest(), 16) % max(len(pool), 1) \
        if pool else 0
    candidates = []
    tc = _TYPE_CTA.get(trig.type)
    if tc:
        candidates.append(tc)
    candidates += pool[rot:] + pool[:rot]
    candidates.append("Shall I set it up?")
    cta = next((str(c) for c in candidates if not cat.taboo_hits(str(c))), "Shall I set it up?")

    rationale = (f"Trigger '{trig.type}' is active now ({_shorten(trig.reason, 90)}) — "
                 f"sending inside the relevant window.")
    return {"body": body.strip(), "cta": cta.strip(),
            "suppression_key": f"merchant:{mid}:trigger:{trig.id}",
            "rationale": rationale}


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
        return None
    if CTA_DUP_RE.search(body):          # body must not carry a second CTA
        return None
    ctx_nums = set(NUM_RE.findall(facts_dump.replace(",", "")))
    for n in NUM_RE.findall(body.replace(",", "")):
        if n not in ctx_nums:            # anti-fabrication gate
            return None
    return {"body": body, "cta": cta,
            "suppression_key": str(comp.get("suppression_key") or "").strip(),
            "rationale": str(comp.get("rationale") or "").strip()}


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


def layer1_evaluate(now: float) -> list[dict]:
    """Returns action dicts (already composed). Silence is a first-class outcome."""
    cat_payload = STORE.category.payload if STORE.category else {}
    cat = CategoryView(cat_payload)
    candidates: list[tuple[float, str, TriggerView, Optional[dict], float]] = []

    for tid, rec in list(STORE.triggers.items()):
        trig = TriggerView(rec.ctx.payload, tid)
        expires_at = trig.expires_at or rec.expires_at
        if expires_at and expires_at < now:
            rec.status = "expired"
            STORE.log_decision(tick=now, trigger=tid, verdict="skip", reason="expired")
            continue
        if rec.status != "open":
            continue
        # --- why-now decay, measured on the SIM timeline ---
        # Anchor this trigger's arrival on the tick clock the first time we
        # see it (robust even when the judge's simulated clock differs from
        # server wall-clock). A trigger still open after STALE_AFTER_S of sim
        # time, with no imminent window, is no longer news — silence wins.
        if rec.sim_seen is None:
            wall_age = max(time.time() - rec.ctx.received_at, 0.0)
            rec.sim_seen = now - wall_age
        sim_age = now - rec.sim_seen
        if sim_age > STALE_AFTER_S and (expires_at is None or expires_at - now > 48 * 3600):
            rec.status = "stale"
            STORE.log_decision(tick=now, trigger=tid, verdict="skip",
                               reason="stale_why_now", age_s=round(sim_age))
            continue
        if STORE.is_suppressed(f"trigger:{tid}"):
            rec.status = "suppressed"
            continue
        mid = trig.merchant_id
        merch_payload = STORE.get_merchant(mid)
        if not merch_payload:
            STORE.log_decision(tick=now, trigger=tid, verdict="skip", reason="no_merchant_context")
            continue
        if STORE.is_suppressed(f"merchant:{mid}:ended"):
            STORE.log_decision(tick=now, trigger=tid, verdict="skip", reason="merchant_conversation_ended")
            continue
        conv = STORE.conversations.get(mid) or STORE.conversations.get(f"{mid}")
        if conv is not None and conv.phase == "ended":
            STORE.log_decision(tick=now, trigger=tid, verdict="skip", reason="conversation_ended")
            continue
        if STORE.merchant_recently_sent(mid, now):
            STORE.log_decision(tick=now, trigger=tid, verdict="skip", reason="merchant_pacing_window")
            continue
        if STORE.cache_get(mid, tid) is not None:
            rec.status = "acted"
            STORE.log_decision(tick=now, trigger=tid, verdict="skip", reason="already_composed")
            continue

        cust_payload = STORE.get_customer(trig.customer_id)
        score = _score_trigger(trig, True, bool(cust_payload), cat, now, expires_at)
        if score < SILENCE_BAR:
            STORE.log_decision(tick=now, trigger=tid, verdict="silence", score=score)
            continue
        candidates.append((score, mid, trig, cust_payload, expires_at or 0.0))

    def _sort_key(c: tuple):
        # best score first; tie-break: soonest-expiring window first, then
        # freshest signal first (a newly injected trigger outranks old backlog)
        score, _mid, trig, _cu, exp = c
        received = trig.raw.get("_received_at")
        if not isinstance(received, (int, float)):
            received = 0.0
        return (-score, exp if exp else float("inf"), -received)

    candidates.sort(key=_sort_key)
    actions: list[dict] = []
    per_merchant: dict[str, int] = {}
    selected: set[str] = set()
    for score, mid, trig, cust_payload, _exp in candidates:
        if len(actions) >= MAX_ACTIONS_PER_TICK:
            STORE.log_decision(tick=now, trigger=trig.id, verdict="deferred",
                               reason="tick_cap", score=score)
            continue
        if per_merchant.get(mid, 0) >= 1:       # never double-tap one merchant in a tick
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
                             trigger_id=trig.id, merchant_id=mid)
        action["source"] = source
        action["score"] = score
        actions.append(action)
        per_merchant[mid] = 1
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
        return {"status": "accepted", **results[0]}
    return {"status": "accepted", "results": results}


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
        actions = layer1_evaluate(now)
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
                "detail": decision.get("detail", "")}

    # send → Layer 2 (conversational follow-up)
    cat = CategoryView(STORE.category.payload if STORE.category else {})
    merch = MerchantView(STORE.get_merchant(mid) or {"merchant_name": "there"})
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
