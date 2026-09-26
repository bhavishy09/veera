#!/usr/bin/env python3
"""Generate submission.jsonl — 30 lines, one per canonical test pair across all 5 categories.

Default mode is OFFLINE (grounded fallback composer, deterministic, no quota).
With `--live` and GEMINI_API_KEYS set, compositions use Gemini Flash.

Output line schema (per brief):
  test_id, body, cta, send_as, suppression_key, rationale
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
os.environ.setdefault("DISABLE_LLM", "1")

import bot  # noqa: E402
from schemas import CategoryView, CustomerView, MerchantView, TriggerView  # noqa: E402
from store import STORE  # noqa: E402

DATASET = os.path.join(ROOT, "dataset")
EXPANDED = os.path.join(DATASET, "expanded")
OUT_PATH = os.path.join(ROOT, "submission.jsonl")

REQUIRED = ("test_id", "body", "cta", "send_as", "suppression_key", "rationale")


def ensure_dataset():
    pairs_file = os.path.join(EXPANDED, "test_pairs.json")
    if not os.path.exists(pairs_file):
        print("Generating expanded dataset with test_pairs.json...")
        subprocess.run([
            sys.executable, os.path.join(DATASET, "generate_dataset.py"),
            "--seed-dir", DATASET, "--out", EXPANDED
        ], check=True)


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


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

    ensure_dataset()

    # Load categories
    categories = {}
    cat_dir = os.path.join(DATASET, "categories")
    for f in os.listdir(cat_dir):
        if f.endswith(".json"):
            slug = f.replace(".json", "")
            categories[slug] = CategoryView(load_json(os.path.join(cat_dir, f)))

    # Load test pairs
    pairs_data = load_json(os.path.join(EXPANDED, "test_pairs.json"))
    pairs = pairs_data["pairs"][:30]

    STORE.reset()
    lines = []

    for pair in pairs:
        mid = pair["merchant_id"]
        tid = pair["trigger_id"]
        cid = pair.get("customer_id")

        # Load merchant
        m_path = os.path.join(EXPANDED, "merchants", f"{mid}.json")
        if os.path.exists(m_path):
            m_raw = load_json(m_path)
        else:
            seeds = load_json(os.path.join(DATASET, "merchants_seed.json"))["merchants"]
            m_raw = next(m for m in seeds if m["merchant_id"] == mid)

        merch = MerchantView(m_raw)
        cat_slug = merch.category_slug or m_raw.get("category_slug", "dentists")
        cat = categories.get(cat_slug, next(iter(categories.values())))

        # Load customer
        cust = None
        if cid:
            c_path = os.path.join(EXPANDED, "customers", f"{cid}.json")
            if os.path.exists(c_path):
                c_raw = load_json(c_path)
            else:
                c_seeds = load_json(os.path.join(DATASET, "customers_seed.json"))["customers"]
                c_raw = next((c for c in c_seeds if c["customer_id"] == cid), None)
            if c_raw:
                cust = CustomerView(c_raw)

        # Load trigger
        t_path = os.path.join(EXPANDED, "triggers", f"{tid}.json")
        if os.path.exists(t_path):
            t_raw = load_json(t_path)
        else:
            t_seeds = load_json(os.path.join(DATASET, "triggers_seed.json"))["triggers"]
            t_raw = next(t for t in t_seeds if t["id"] == tid)

        trig = TriggerView(t_raw, tid)

        comp, _source = bot.compose(cat, merch, trig, cust, mid)
        send_as = "merchant" if (cust is not None and trig.customer_id) else "platform"

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
