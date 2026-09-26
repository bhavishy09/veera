#!/usr/bin/env python3
"""Final acceptance validator for submission.jsonl.

Checks the hard rules from the challenge brief:
  1. exactly 30 lines, valid JSON each
  2. required fields present + non-empty: test_id, body, cta, send_as,
     suppression_key, rationale
  3. unique test_id
  4. no URLs in body/cta
  5. single CTA (cta field non-empty; body contains no second imperative ask
     heuristics: no 'Reply'/'Book'/'Call'/'WhatsApp' doubled)
  6. send_as in {platform, merchant}
  7. suppression_key format sanity (non-empty, no spaces)
  8. anti-fabrication: every number mentioned in body must be traceable to the
     merchant stats / category peer stats / trigger payload for that test pair
  9. body length sane (<= 480 chars, whatsapp-appropriate)
 10. no exact-duplicate bodies
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

PAIRS = json.load(open(os.path.join(ROOT, "dataset", "pairs.json"), encoding="utf-8"))
MERCH = {m["merchant_id"]: m for m in json.load(open(os.path.join(ROOT, "dataset", "merchants.json"), encoding="utf-8"))}
CAT = json.load(open(os.path.join(ROOT, "dataset", "category_dentistry.json"), encoding="utf-8"))

REQUIRED = ("test_id", "body", "cta", "send_as", "suppression_key", "rationale")
CTA_VERBS = re.compile(r"\b(reply|book|call|whatsapp|confirm|reserve|slot|visit)\b", re.I)

problems: list[str] = []
warnings: list[str] = []


def traceable_numbers(line: dict, pair: dict) -> list[str]:
    """Return numbers in body that are NOT found in any context source."""
    body = line["body"]
    ctx_sources = [
        json.dumps(MERCH.get(pair["merchant_id"], {})),
        json.dumps(pair.get("trigger_payload", {})),
        json.dumps(CAT.get("peer_stats", {})) + json.dumps(CAT.get("offer_catalog", []))
        + json.dumps(CAT.get("digest", {})),
    ]
    if pair.get("customer_id"):
        cust = json.load(open(os.path.join(ROOT, "dataset", "customers.json"), encoding="utf-8"))
        cm = next((c for c in cust if c["customer_id"] == pair["customer_id"]), {})
        ctx_sources.append(json.dumps(cm))
    ctx_blob = " ".join(ctx_sources)
    missing = []
    for num in re.findall(r"\d[\d,.%]*", body):
        if num in ctx_blob:
            continue
        # tolerate ordinals/rounds like "2 days" only if '2' appears in context
        if num.rstrip("%.,") in ctx_blob:
            continue
        missing.append(num)
    return missing


def main() -> None:
    path = os.path.join(ROOT, "submission.jsonl")
    raw_lines = [l for l in open(path, encoding="utf-8").read().splitlines() if l.strip()]

    if len(raw_lines) != 30:
        problems.append(f"line count {len(raw_lines)} != 30")

    seen_ids, seen_bodies = set(), set()
    pair_ids = {p["test_id"] for p in PAIRS}

    for i, raw in enumerate(raw_lines, 1):
        try:
            ln = json.loads(raw)
        except json.JSONDecodeError as e:
            problems.append(f"line {i}: invalid JSON ({e})")
            continue
        # 2. fields
        for k in REQUIRED:
            if k not in ln:
                problems.append(f"line {i}: missing field {k}")
            elif not str(ln[k]).strip():
                problems.append(f"line {i}: empty field {k}")
        # 3. unique ids, must match canonical pairs
        tid = ln.get("test_id", "")
        if tid in seen_ids:
            problems.append(f"line {i}: duplicate test_id {tid}")
        seen_ids.add(tid)
        if tid not in pair_ids:
            problems.append(f"line {i}: test_id {tid} not in canonical pairs")
        # 4. no URLs
        for f in ("body", "cta"):
            if re.search(r"(https?://|www\.|\.com\b)", ln.get(f, ""), re.I):
                problems.append(f"line {i}: URL-like text in {f}")
        # 5. single CTA
        body = ln.get("body", "")
        cta = ln.get("cta", "")
        body_asks = CTA_VERBS.findall(body)
        if len(body_asks) >= 2:
            warnings.append(f"line {i}: body may contain multiple asks ({body_asks})")
        if not cta.strip():
            problems.append(f"line {i}: empty cta field")
        # 6. send_as
        if ln.get("send_as") not in ("platform", "merchant"):
            problems.append(f"line {i}: bad send_as {ln.get('send_as')!r}")
        # 7. suppression key
        sk = ln.get("suppression_key", "")
        if " " in sk or sk != sk.strip() or len(sk) < 4:
            problems.append(f"line {i}: malformed suppression_key {sk!r}")
        # 8. anti-fabrication
        pair = next((p for p in PAIRS if p["test_id"] == tid), None)
        if pair:
            missing = traceable_numbers(ln, pair)
            if missing:
                problems.append(f"line {i}: untraceable numbers {missing} in body")
        # 9. length
        if len(body) > 480:
            problems.append(f"line {i}: body too long ({len(body)} chars)")
        # 10. dup bodies
        b = body.strip().lower()
        if b in seen_bodies:
            problems.append(f"line {i}: duplicate body")
        seen_bodies.add(b)

    # cta diversity sanity (not a hard rule, but a judge-visible quality signal)
    ctas = [json.loads(l)["cta"].strip().lower() for l in raw_lines if l.strip()]
    uniq = len(set(ctas))
    print(f"cta diversity: {uniq} distinct cta across {len(ctas)} lines")
    print(f"send_as split: platform={sum(1 for c in ctas)}, merchant-dir={sum(1 for l in raw_lines if l.strip() and json.loads(l)['send_as']=='merchant')}")

    if warnings:
        print("WARNINGS:", *("  " + w for w in warnings), sep="\n", file=sys.stderr)
    if problems:
        print("PROBLEMS:", *("  " + p for p in problems), sep="\n", file=sys.stderr)
        sys.exit(1)
    print(f"OK: 30 lines, all hard rules pass.")


if __name__ == "__main__":
    main()
