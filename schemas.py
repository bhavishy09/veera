"""Vera — wire schemas + tolerant normalization.

⚠ SCHEMA ADAPTER POINT: this is the ONLY file that needs edits when the exact
wire shapes from api-call-examples.md / dataset/*.json are confirmed. Request
parsing is deliberately tolerant (field aliases, inferred scope) and response
building is conservative (superset of plausible required keys).

Design rules:
- Never 5xx on well-formed-but-unexpected input; ignore unknown fields.
- Normalize all four context payloads into *View objects used by Layer 1/2.
"""
from __future__ import annotations

import re
import time
import datetime as _dt
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# --------------------------------------------------------------------------
# generic helpers
# --------------------------------------------------------------------------

def _pick(d: dict, *names: str, default: Any = None) -> Any:
    """Case-insensitive key lookup across alias names (also checks snake/space variants)."""
    if not isinstance(d, dict):
        return default
    lowered = {str(k).strip().lower().replace(" ", "_").replace("-", "_"): v for k, v in d.items()}
    for n in names:
        if n in lowered and lowered[n] not in (None, ""):
            return lowered[n]
    return default


def _to_epoch(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if float(v) > 1e12 else float(v)  # epoch s; ms handled below
    s = str(v).strip()
    if not s:
        return None
    try:
        n = float(s)
        return n / 1000.0 if n > 1e12 else n
    except ValueError:
        pass
    try:
        s2 = s.replace("Z", "+00:00")
        return _dt.datetime.fromisoformat(s2).timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# /v1/context — tolerant push model
# --------------------------------------------------------------------------

SCOPE_ALIASES = {
    "category": {"category", "categorycontext", "vertical"},
    "merchant": {"merchant", "merchantcontext", "business", "shop"},
    "customer": {"customer", "customercontext", "patient", "endcustomer"},
    "trigger": {"trigger", "triggercontext", "event", "signal"},
}


def _infer_scope(data: dict) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    if _pick(data, "voice", "tone", "taboos", "offer_catalog", "offers", "seasonal_beats") is not None \
            and _pick(data, "trigger_type", "reason", "expiry") is None:
        return "category"
    if _pick(data, "merchant_name", "shop_name", "business_name", "clinic_name") is not None:
        return "merchant"
    if _pick(data, "customer_name", "patient_name", "phone", "last_visit") is not None \
            and _pick(data, "trigger_type") is None:
        return "customer"
    if _pick(data, "trigger_type", "trigger_id", "reason", "why_now", "expires_at", "urgency") is not None:
        return "trigger"
    return None


class ContextPush(BaseModel):
    model_config = {"extra": "ignore"}

    scope: str
    context_id: str
    version: int = 1
    data: dict = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _tolerate_aliases(cls, values: Any):
        if not isinstance(values, dict):
            return values
        v = dict(values)
        # scope: accept "scope" | "type" | "kind" | "context_type"
        if not _pick(v, "scope"):
            t = _pick(v, "type", "kind", "context_type")
            if t:
                v["scope"] = str(t).strip().lower()
        # context_id: accept "context_id" | "id" | "name"
        if not _pick(v, "context_id"):
            cid = _pick(v, "id", "context", "key", "name")
            if cid is not None:
                v["context_id"] = str(cid)
        # data: accept "data" | "payload" | "context"(dict) | nested under scope name
        if not isinstance(v.get("data"), dict):
            for alt in ("payload", "content", "body", "fields"):
                if isinstance(v.get(alt), dict):
                    v["data"] = v[alt]
                    break
            else:
                # maybe the whole push IS the payload, with scope/id alongside
                v["data"] = {k: val for k, val in v.items()
                             if k not in ("scope", "type", "kind", "context_type",
                                          "context_id", "id", "version", "payload",
                                          "content", "body", "fields")}
        # default id/version if absent
        if not _pick(v, "context_id"):
            v["context_id"] = _pick(v["data"], "id", "name", "merchant_id", "trigger_id",
                                    "customer_id", "category") or "anon_" + str(abs(hash(_dump(v["data"]))) % 10**8)
        try:
            int(v.get("version", 1))
        except (TypeError, ValueError):
            v["version"] = 1
        return v

    @field_validator("scope")
    @classmethod
    def _norm_scope(cls, s: str) -> str:
        s = (s or "").strip().lower().replace(" ", "_").replace("-", "_")
        if s in ("categorycontext", "vertical"):
            s = "category"
        if s in ("merchantcontext", "business", "shop"):
            s = "merchant"
        if s in ("customercontext", "patient", "endcustomer"):
            s = "customer"
        if s in ("triggercontext", "event", "signal"):
            s = "trigger"
        return s


def _dump(x: Any) -> str:
    try:
        import json
        return json.dumps(x, sort_keys=True, default=str)
    except Exception:
        return str(x)


def normalize_push(push: ContextPush) -> ContextPush:
    """If scope was omitted/unresolvable, infer from payload shape."""
    if push.scope not in ("category", "merchant", "customer", "trigger"):
        inferred = _infer_scope(push.data)
        if inferred:
            push.scope = inferred
    return push


# --------------------------------------------------------------------------
# View objects used by decision engine + composer
# --------------------------------------------------------------------------

class CategoryView:
    def __init__(self, payload: dict):
        self.raw = payload or {}
        self.voice = _pick(self.raw, "voice", "tone", "voice_tone", "brand_voice") or {}
        if isinstance(self.voice, str):
            self.voice = {"tone": self.voice}
        self.tone: str = str(_pick(self.voice if isinstance(self.voice, dict) else {}, "tone", "style") or self.voice or "warm, professional, concise")
        self.style_notes: str = str(_pick(self.voice if isinstance(self.voice, dict) else {}, "style_notes", "notes", "guidelines", "register") or "")
        taboos = _pick(self.raw, "taboos", "taboo", "avoid", "avoid_list", "donts", "forbidden")
        if not taboos and isinstance(self.voice, dict):
            taboos = _pick(self.voice, "vocab_taboo", "taboos", "taboo")
        self.taboos: list[str] = [str(t) for t in taboos] if isinstance(taboos, list) else ([str(taboos)] if taboos else [])
        offers = _pick(self.raw, "offer_catalog", "offers", "offer", "campaigns", "promos")
        self.offers: list[dict] = [o for o in offers if isinstance(o, dict)] if isinstance(offers, list) else []
        if isinstance(offers, list) and offers and not isinstance(offers[0], dict):
            self.offers = [{"title": str(o)} for o in offers]
        self.peer_stats: dict = _pick(self.raw, "peer_stats", "peerstats", "benchmarks", "category_stats") or {}
        if not isinstance(self.peer_stats, dict):
            self.peer_stats = {}
        self.digest: Any = _pick(self.raw, "digest", "digest_config", "digest_settings") or {}
        self.seasonal: list = _pick(self.raw, "seasonal_beats", "seasonal", "seasons") or []
        if not isinstance(self.seasonal, list):
            self.seasonal = []
        self.cta_preferences: list[str] = []
        cta_pref = _pick(self.raw, "cta_preferences", "cta_pref", "preferred_ctas", "cta_style")
        if isinstance(cta_pref, list):
            self.cta_preferences = [str(c) for c in cta_pref]
        elif isinstance(cta_pref, str):
            self.cta_preferences = [cta_pref]
        self.name: str = str(_pick(self.raw, "slug", "display_name", "category", "vertical", "name", "category_name") or "this category")

    def taboo_hits(self, text: str) -> list[str]:
        t = (text or "").lower()
        hits = []
        for taboo in self.taboos:
            taboo_l = taboo.lower().strip()
            # match on the salient words of the taboo phrase
            words = [w for w in re.findall(r"[a-z]{4,}", taboo_l) if w not in ("language", "about", "more", "than", "claims", "problems", "when", "actually", "applicable")]
            if words and all(w in t for w in words[:2]):
                hits.append(taboo)
            elif len(words) == 1 and words[0] in t:
                hits.append(taboo)
        return hits


class MerchantView:
    def __init__(self, payload: dict):
        self.raw = payload or {}
        ident = _pick(self.raw, "identity", "profile", "business")
        if isinstance(ident, dict):
            self.name: str = str(_pick(ident, "name", "owner_first_name", "business_name", "clinic_name") or
                                 _pick(self.raw, "merchant_name", "name", "business_name") or "there")
        else:
            self.name = str(_pick(self.raw, "merchant_name", "name", "business_name", "clinic_name") or "there")
        self.id: Optional[str] = _pick(self.raw, "merchant_id", "id", "shop_id", "business_id")
        self.category_slug: Optional[str] = _pick(self.raw, "category_slug", "category", "vertical")
        stats = _pick(self.raw, "stats", "metrics", "performance", "kpis")
        self.stats: dict = stats if isinstance(stats, dict) else {}
        self.history: Any = _pick(self.raw, "history", "recent_events", "timeline", "activity", "conversation_history")
        self.raw_facts = self.raw  # full payload available for grounding


class CustomerView:
    def __init__(self, payload: dict):
        self.raw = payload or {}
        ident = _pick(self.raw, "identity", "profile")
        if isinstance(ident, dict):
            self.name: str = str(_pick(ident, "name", "first_name") or _pick(self.raw, "customer_name", "patient_name", "name") or "")
        else:
            self.name = str(_pick(self.raw, "customer_name", "patient_name", "name", "first_name") or "")
        self.id: Optional[str] = _pick(self.raw, "customer_id", "id", "patient_id", "phone")
        self.history: Any = _pick(self.raw, "history", "visit_history", "last_visit", "relationship", "recent_activity", "notes")


class TriggerView:
    def __init__(self, payload: dict, trigger_id: str):
        self.raw = payload or {}
        self.id: str = str(_pick(self.raw, "trigger_id", "id") or trigger_id)
        ttype = _pick(self.raw, "trigger_type", "type", "kind", "event", "signal_type", "name")
        self.type: str = str(ttype) if ttype else "general"
        reason = _pick(self.raw, "reason", "message", "description", "detail", "details", "why_now", "summary", "note", "text")
        if not reason and isinstance(self.raw.get("payload"), dict):
            parts = [f"{k.replace('_', ' ')}: {v}" for k, v in self.raw["payload"].items() if not isinstance(v, (list, dict))]
            self.reason = f"{self.type.replace('_', ' ')} — {', '.join(parts)}" if parts else self.type.replace('_', ' ')
        else:
            self.reason = str(reason) if reason else self.type.replace('_', ' ')
        self.urgency: Optional[str] = None
        u = _pick(self.raw, "urgency", "priority", "severity", "importance")
        if isinstance(u, (int, float)):
            if u >= 4:
                self.urgency = "high"
            elif u >= 2:
                self.urgency = "medium"
            else:
                self.urgency = "low"
        elif u is not None:
            self.urgency = str(u).strip().lower()
        else:
            self.urgency = "medium"
        self.expires_at: Optional[float] = _to_epoch(
            _pick(self.raw, "expires_at", "expiry", "valid_until", "deadline", "expires", "end_time"))
        expires_in = _pick(self.raw, "expires_in_hours", "valid_hours", "ttl_hours")
        if self.expires_at is None and expires_in is not None:
            try:
                self.expires_at = time.time() + float(expires_in) * 3600.0
            except (TypeError, ValueError):
                pass
        self.merchant_id: Optional[str] = _pick(self.raw, "merchant_id", "shop_id", "merchant", "business_id")
        if isinstance(self.merchant_id, dict):
            self.merchant_id = _pick(self.merchant_id, "merchant_id", "id")
        self.customer_id: Optional[str] = _pick(self.raw, "customer_id", "patient_id", "customer", "patient")
        if isinstance(self.customer_id, dict):
            self.customer_id = _pick(self.customer_id, "customer_id", "id")
        self.payload_for_prompt = self.raw


# --------------------------------------------------------------------------
# /v1/tick, /v1/reply — tolerant request models
# --------------------------------------------------------------------------

class TickRequest(BaseModel):
    model_config = {"extra": "ignore"}
    now: Optional[float | str | int] = None
    tick_id: Optional[str] = None
    available_triggers: Optional[list[str]] = None

    @model_validator(mode="before")
    @classmethod
    def _tolerate(cls, values: Any):
        if isinstance(values, dict):
            v = dict(values)
            if v.get("now") is None:
                alt = _pick(v, "timestamp", "sim_time", "simulated_time", "time", "current_time")
                if alt is not None:
                    v["now"] = alt
            return v
        return values

    def epoch(self, fallback: float) -> float:
        e = _to_epoch(self.now)
        return e if e is not None else fallback


class ReplyRequest(BaseModel):
    model_config = {"extra": "ignore"}
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    text: str = ""
    message_id: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _tolerate(cls, values: Any):
        if not isinstance(values, dict):
            return values
        v = dict(values)
        if not _pick(v, "text"):
            msg = v.get("message")
            if isinstance(msg, dict):
                t = _pick(msg, "text", "body", "content")
                if t is not None:
                    v["text"] = str(t)
                if v.get("customer_id") is None:
                    v["customer_id"] = _pick(msg, "customer_id", "from", "sender")
            elif isinstance(msg, str):
                v["text"] = msg
            else:
                t = _pick(v, "body", "content", "reply", "text")
                if t is not None:
                    v["text"] = str(t)
        if v.get("merchant_id") is None:
            v["merchant_id"] = _pick(v, "merchant", "shop_id", "business_id")
        return v


# --------------------------------------------------------------------------
# response builders (conservative supersets)
# --------------------------------------------------------------------------

def action_dict(body: str, cta: str, suppression_key: str, rationale: str,
                to: Optional[str], send_as: str, trigger_id: Optional[str] = None,
                merchant_id: Optional[str] = None, customer_id: Optional[str] = None) -> dict:
    d = {
        "type": "send_message",
        "send_as": send_as,
        "to": to,
        "body": body,
        "cta": cta,
        "suppression_key": suppression_key,
        "rationale": rationale,
    }
    if trigger_id:
        d["trigger_id"] = trigger_id
    if merchant_id:
        d["merchant_id"] = merchant_id
    if customer_id:
        d["customer_id"] = customer_id
    return d


def error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}
