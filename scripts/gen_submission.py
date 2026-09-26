#!/usr/bin/env python3
"""Generate submission.jsonl — 30 lines, one per canonical test pair.

Default mode is OFFLINE (grounded fallback composer, deterministic, no quota).
With `--live` and GEMINI_API_KEYS set, compositions use Gemini Flash.

Output line schema (per brief):
  test_id, body, cta, send_as, suppression_key, rationale

⚠ If the real challenge dataset generator arrives, this script should be
re-pointed at ITS canonical pairs; the composition path stays identical.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
os.environ.setdefault("DISABLE_LLM", "1")

import bot  # noqa: E402
from schemas import CategoryView, CustomerView, MerchantView, TriggerView  # noqa: E402
from store import STORE  # noqa: E402

DATASET = os.path.join(ROOT, "dataset")
OUT_PATH = os.path.join(ROOT, "submission.jsonl")

REQUIRED = ("test_id", "body", "cta", "send_as", "suppression_key", "rationale")


def load(name):
    with open(os.path.join(DATASET, name), encoding="utf-8") as fh:
        return json.load(fh)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="use Gemini Flash (requires GEMINI_API_KEYS); default offline fallback")
    args = ap.parse_args()

    if args.live:
        os.environ.pop("DISABLE_LLM", None)
        bot.DISABLE_LLM = False
        if not bot.GEMINI_API_KEYS:
            print("WARNING: --live requested but no GEMINI_API_KEYS configured; "
                  "falling back to offline compositions.", file=sys.stderr)

    category = load("category_dentistry.json")
    merchants = {m["merchant_id"]: m for m in load("merchants.json")}
    customers = {c["customer_id"]: c for c in load("customers.json")}
    pairs = load("pairs.json")

    STORE.reset()
    cat = CategoryView(category)

    lines = []
    for pair in pairs:
        mid = pair["merchant_id"]
        merch = MerchantView(merchants[mid])
        cust = CustomerView(customers[pair["customer_id"]]) if pair.get("customer_id") else None
        trig = TriggerView(pair["trigger_payload"], pair["trigger_id"])
        comp, _source = bot.compose(cat, merch, trig, cust, mid)
        send_as = "merchant" if cust is not None else "platform"
        lines.append({
            "test_id": pair["test_id"],
            "body": comp["body"],
            "cta": comp["cta"],
            "send_as": send_as,
            "suppression_key": comp["suppression_key"],
            "rationale": comp["rationale"],
        })

    # ---- self-validation -------------------------------------------------
    problems = []
    seen_ids = set()
    for i, ln in enumerate(lines, 1):
        for k in REQUIRED:
            if not ln.get(k):
                problems.append(f"line {i}: empty field {k}")
        if ln["test_id"] in seen_ids:
            problems.append(f"line {i}: duplicate test_id {ln['test_id']}")
        seen_ids.add(ln["test_id"])
        for field in ("body", "cta"):
            if "http" in ln[field].lower() or "www." in ln[field].lower():
                problems.append(f"line {i}: URL in {field}")
    assert len(lines) == 30, f"expected 30 lines, got {len(lines)}"
    if problems:
        print("VALIDATION PROBLEMS:", *problems, sep="\n  ", file=sys.stderr)
        sys.exit(1)

    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(json.dumps(ln, ensure_ascii=False) + "\n")
    print(f"wrote {OUT_PATH}: 30 lines OK (mode={'live' if args.live else 'offline-fallback'})")


if __name__ == "__main__":
    main()
