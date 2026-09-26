"""Vera — in-memory state layer.

Single-process, thread-safe (judge drives one HTTP client; uvicorn single
worker per spec). No persistence by design: spec allows in-memory state and
forbids restarts mid-test. POST /v1/teardown wipes everything.

Idempotency contract (/v1/context): key = (scope, context_id, version).
- same or lower version than stored  -> ACK without mutation
- higher version                     -> replace payload (latest wins)
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class VersionedContext:
    scope: str                 # "category" | "merchant" | "customer" | "trigger"
    context_id: str
    version: int
    payload: dict              # normalized raw JSON (unknown fields preserved)
    received_at: float


@dataclass
class TriggerRecord:
    ctx: VersionedContext
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    expires_at: Optional[float] = None
    status: str = "open"       # open | acted | expired | stale | suppressed
    # sim-timeline anchor: the tick clock value when this trigger was first
    # observed (lets "age" be measured on the judge's simulated clock even if
    # it differs from server wall-clock)
    sim_seen: Optional[float] = None


@dataclass
class SuppressionRecord:
    key: str
    reason: str                # composed | ended | hostile | auto_reply | manual
    created_at: float
    merchant_id: Optional[str] = None
    trigger_id: Optional[str] = None


@dataclass
class CachedComposition:
    body: str
    cta: str
    suppression_key: str
    rationale: str
    source: str                # llm | fallback
    composed_at: float
    send_as: str = "platform"
    to: Optional[str] = None


@dataclass
class ConversationState:
    key: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    phase: str = "idle"        # idle | awaiting | engaged | ended
    auto_reply_count: int = 0
    hostility_signals: int = 0
    pending_intent: Optional[str] = None
    last_outbound: Optional[dict] = None
    history: list = field(default_factory=list)
    ended_reason: Optional[str] = None
    followup_count: int = 0


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.started_at = time.time()
        self.category: Optional[VersionedContext] = None
        self.categories: dict[str, VersionedContext] = {}
        self.merchants: dict[str, VersionedContext] = {}
        self.customers: dict[str, VersionedContext] = {}
        self.triggers: dict[str, TriggerRecord] = {}
        self._seen_versions: dict[tuple[str, str], int] = {}
        self._replays_acknowledged = 0
        self.suppressions: dict[str, SuppressionRecord] = {}
        self.composition_cache: dict[tuple[str, str], CachedComposition] = {}
        self.conversations: dict[str, ConversationState] = {}
        self.decision_log: deque = deque(maxlen=500)
        self.last_send_per_merchant: dict[str, float] = {}

    # ---------- context ingestion ----------
    def upsert_context(self, scope: str, context_id: str, version: int,
                       payload: dict) -> tuple[bool, bool]:
        """Returns (accepted, is_new). Idempotent by (scope, context_id, version)."""
        with self._lock:
            key = (scope, context_id)
            stored_version = self._seen_versions.get(key)
            if stored_version is not None and version <= stored_version:
                self._replays_acknowledged += 1
                return (False, False)          # duplicate/older replay -> ACK, no-op
            vc = VersionedContext(scope, context_id, version, payload, time.time())
            self._seen_versions[key] = version
            if scope == "category":
                self.category = vc
                self.categories[context_id] = vc
            elif scope == "merchant":
                self.merchants[context_id] = vc
            elif scope == "customer":
                self.customers[context_id] = vc
            elif scope == "trigger":
                rec = self.triggers.get(context_id)
                if rec is None:
                    rec = TriggerRecord(ctx=vc)
                    self.triggers[context_id] = rec
                else:
                    rec.ctx = vc
                    rec.status = "open"
                self.triggers[context_id].ctx = vc
            return (True, True)

    # ---------- lookups ----------
    def get_category(self, slug: Optional[str] = None) -> Optional[dict]:
        if slug and slug in self.categories:
            return self.categories[slug].payload
        if slug:
            for k, v in self.categories.items():
                if slug in k or k in slug:
                    return v.payload
        if self.category:
            return self.category.payload
        if self.categories:
            return next(iter(self.categories.values())).payload
        return None

    def get_merchant(self, merchant_id: Optional[str]) -> Optional[dict]:
        if merchant_id and merchant_id in self.merchants:
            return self.merchants[merchant_id].payload
        return None

    def get_customer(self, customer_id: Optional[str]) -> Optional[dict]:
        if customer_id and customer_id in self.customers:
            return self.customers[customer_id].payload
        return None

    # ---------- suppression ----------
    def register_suppression(self, key: str, reason: str,
                             merchant_id: Optional[str] = None,
                             trigger_id: Optional[str] = None) -> None:
        with self._lock:
            if key not in self.suppressions:
                self.suppressions[key] = SuppressionRecord(
                    key, reason, time.time(), merchant_id, trigger_id)
            for tid, rec in self.triggers.items():
                if trigger_id and tid == trigger_id and rec.status == "open":
                    rec.status = "suppressed"

    def is_suppressed(self, key: str) -> bool:
        return key in self.suppressions

    # ---------- composition cache ----------
    def cache_get(self, merchant_id: str, trigger_id: str) -> Optional[CachedComposition]:
        return self.composition_cache.get((merchant_id, trigger_id))

    def cache_put(self, merchant_id: str, trigger_id: str, comp: CachedComposition) -> None:
        with self._lock:
            self.composition_cache[(merchant_id, trigger_id)] = comp

    # ---------- merchant send pacing ----------
    def merchant_recently_sent(self, merchant_id: str, now: float) -> bool:
        last = self.last_send_per_merchant.get(merchant_id)
        from config import MERCHANT_WINDOW_S, MAX_SENDS_PER_MERCHANT_WINDOW
        if last is None:
            return False
        # Phase-1 pacing: at most MAX_SENDS per window (we track last send only;
        # window guard blocks any additional send within MERCHANT_WINDOW_S).
        return MAX_SENDS_PER_MERCHANT_WINDOW <= 1 and (now - last) < MERCHANT_WINDOW_S

    def mark_sent(self, merchant_id: str, now: float) -> None:
        with self._lock:
            self.last_send_per_merchant[merchant_id] = now

    # ---------- conversations ----------
    def conversation(self, key: str, merchant_id: Optional[str] = None,
                     customer_id: Optional[str] = None) -> ConversationState:
        with self._lock:
            if key not in self.conversations:
                self.conversations[key] = ConversationState(
                    key, merchant_id=merchant_id, customer_id=customer_id)
            return self.conversations[key]

    # ---------- misc ----------
    def counts(self) -> dict:
        with self._lock:
            return {
                "category": len(self.categories) if self.categories else (1 if self.category else 0),
                "merchant": len(self.merchants),
                "customer": len(self.customers),
                "trigger": len(self.triggers),
                "trigger_open": sum(1 for t in self.triggers.values() if t.status == "open"),
                "suppressions": len(self.suppressions),
                "cached_compositions": len(self.composition_cache),
                "conversations": len(self.conversations),
                "replays_acknowledged": self._replays_acknowledged,
            }

    def uptime_s(self) -> float:
        return time.time() - self.started_at

    def log_decision(self, **entry: Any) -> None:
        entry["ts"] = time.time()
        self.decision_log.append(entry)

    def reset(self) -> None:
        with self._lock:
            self.__init__()  # type: ignore[misc]


STORE = Store()
