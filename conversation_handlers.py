"""Vera — conversation_handlers.py

Bonus-module scope: multi-turn state machine for incoming merchant/customer
replies. Pure logic — NO LLM calls and no I/O. The caller (bot.py) invokes the
composer only when the returned decision is "send".

Public API (per challenge brief):
    respond(state, merchant_message) -> dict
with keys:
    response : "send" | "wait" | "end"
    reason   : machine-readable reason string
    detail   : human-readable explanation (also useful for the judge log)

Handled reply classes:
    1. auto-reply detection   (repeated machine-like acks)        -> wait
    2. intent-transition      (explicit commitment)               -> end (log intent)
    3. hostility / off-topic  (opt-out, abuse, wrong number)      -> end (suppress)
    4. genuine interest       (questions / substantive replies)   -> send
"""
from __future__ import annotations

import re
from typing import Any

from config import AUTO_REPLY_THRESHOLD, HOSTILITY_THRESHOLD

# --- auto-reply heuristics -------------------------------------------------
_ACK_WORDS = {
    "ok", "okay", "k", "kk", "okk", "thx", "thanks", "thank", "you", "ty", "noted",
    "done", "fine", "yes", "no", "maybe", "later", "good", "great", "nice", "cool",
    "alright", "yo", "hi", "hello", "hey", "hmm", "hm", "hmmm", "sure", "sir",
    "mam", "ma'am", "boss", "ok sir", "okka", "haan", "ji", "acha", "theek", "👍", "🙏", "🙂", "👌",
}
_BUSY_PATTERNS = re.compile(
    r"\b(busy|in surgery|with a patient|in a procedure|operating|mid-appointment|call (me )?later"
    r"|will check|let me check|get back (to )?(you|later)|later (today|tonight)?|not now|maybe later|after \d)\b",
    re.IGNORECASE,
)

# --- hostility / off-topic / opt-out ---------------------------------------
_HOSTILE_PATTERNS = re.compile(
    r"\b(stop|stop messaging|don'?t message|do not message|don'?t contact|never contact|"
    r"unsubscribe|remove me|take me off|opt out|wrong number|who is this|harass(ing|ment)?|"
    r"annoy(ing|ed)?|report you|block(ing)? you|useless|worst|scam|fraud|idiot|stupid|nonsense|"
    r"not interested|no thanks,?( we'?re| i'?m)? (not|fine)|don'?t want)\b",
    re.IGNORECASE,
)

# --- explicit commitment -----------------------------------------------------
_COMMIT_PATTERNS = re.compile(
    r"\b(let'?s do it|lets do it|go ahead|go ahead and|confirm|confirmed|book (it|us|the)?|"
    r"booked|yes,? (please|do|go)|count (me|us) in|sounds good|i('| a)m in|do it|"
    r"send (me )?(the )?details|share (the )?details|send (me )?the (link|quote|plan)|we'?ll take it)\b",
    re.IGNORECASE,
)

# --- genuine interest --------------------------------------------------------
_INTEREST_PATTERNS = re.compile(
    r"(\?|how (much|many|do|does|can|about|soon)|what (about|is|are|time|days|s the)"
    r"|when (can|is|are|do)|where (can|is|are)|which (days|time|slot)|price|pricing|cost|"
    r"charges?|fee|fees|timing|timings|availability|available|slot|slots|appointment|"
    r"consult(ation)?|quote|plan|offer|camp|details?|more info|tell me more|know more|"
    r"interesting|sounds interesting|worth (a )?try|can you|could you|is it possible)\b",
    re.IGNORECASE,
)


def _is_ack(text: str) -> bool:
    tokens = [t for t in re.findall(r"[\w']+|[👍🙏🙂👌]", text.lower()) if t]
    if not tokens or len(tokens) > 4:
        return False
    return all(t in _ACK_WORDS for t in tokens)


def _classify(message: str) -> str:
    """Return one of: ack | busy | hostile | commit | interest | substantive | empty."""
    text = (message or "").strip()
    if not text:
        return "empty"
    if _HOSTILE_PATTERNS.search(text):
        return "hostile"
    if _COMMIT_PATTERNS.search(text):
        return "commit"
    if _is_ack(text):
        return "ack"
    if _BUSY_PATTERNS.search(text) and len(text) <= 80 and not _INTEREST_PATTERNS.search(text):
        return "busy"
    if _INTEREST_PATTERNS.search(text):
        return "interest"
    return "substantive"


def respond(state: Any, merchant_message: str) -> dict:
    """Advance the conversation state machine.

    `state` is a store.ConversationState; mutated in place (counters, phase,
    history). Returns the decision dict — "send" lets the caller compose.
    """
    text = (merchant_message or "").strip()
    state.history.append({"dir": "in", "text": text[:400]})

    kind = _classify(text)

    if kind == "empty":
        state.auto_reply_count += 1
        return _wait(state, "auto_reply", "Empty/blank reply — not engaging until substance arrives.")

    if kind == "hostile":
        state.hostility_signals += 1
        if state.hostility_signals >= HOSTILITY_THRESHOLD:
            state.phase = "ended"
            state.ended_reason = "hostile_or_optout"
            return {"response": "end", "reason": "hostile_or_optout",
                    "detail": "Opt-out/hostility detected — conversation ended and suppressed; bot must not re-introduce itself."}

    if kind == "ack" or kind == "busy":
        state.auto_reply_count += 1
        if state.auto_reply_count >= AUTO_REPLY_THRESHOLD:
            state.phase = "awaiting"
            return _wait(state, "auto_reply_repeat",
                         f"{state.auto_reply_count} consecutive machine-like replies — backing off.")
        return _wait(state, "auto_reply", "Ack-only reply — waiting for substance.")

    if kind == "commit":
        state.pending_intent = "committed"
        state.phase = "ended"
        state.ended_reason = "intent_committed"
        return {"response": "end", "reason": "intent_committed",
                "detail": "Explicit commitment detected — no pushy re-pitch; intent logged for the merchant."}

    # interest or substantive -> engage
    state.auto_reply_count = 0
    state.phase = "engaged"
    if kind == "interest":
        state.pending_intent = state.pending_intent or "question"
        return {"response": "send", "reason": "engaged",
                "detail": "Genuine interest/question detected — composing a helpful, single-CTA follow-up."}
    return {"response": "send", "reason": "engaged",
            "detail": "Substantive reply — composing a contextual follow-up."}


def _wait(state: Any, reason: str, detail: str) -> dict:
    state.phase = state.phase if state.phase in ("awaiting", "engaged") else "awaiting"
    return {"response": "wait", "reason": reason, "detail": detail}


def note_outbound(state: Any, body: str) -> None:
    """Caller records what we sent so auto-reply detection has context."""
    state.followup_count += 1
    state.last_outbound = {"body": body[:400]}
    state.history.append({"dir": "out", "text": body[:400]})
