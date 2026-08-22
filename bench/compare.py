"""Compare a benchmark result (or set of results) against a baseline.

Supports two forms of baseline:
- A single record JSON (one run)
- A "usp-bench-baseline" aggregator JSON (set of runs, keyed by profile+cap)

For each candidate, finds the matching baseline run by (profile, fanout_cap) and
compares urls_per_s and peak_rss_mb. Exits 0 if no regression greater than the
configured thresholds, non-zero otherwise.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RESULTS_DIR = Path(__file__).parent / "results"


def load(path: Path):
    with path.open() as f:
        return json.load(f)


def to_record(obj):
    """Normalize aggregator entries to record dict."""
    return {
        "profile": obj["profile"],
        "fanout_cap": obj["fanout_cap"],
        "urls_per_s": obj["urls_per_s_median"],
        "peak_rss_mb": obj["peak_rss_mb_median"],
        "urls_total": obj["urls_total"],
    }


def find_baseline(baseline_obj, profile: str, cap: int):
    if isinstance(baseline_obj, dict) and baseline_obj.get("kind") == "usp-bench-baseline":
        for r in baseline_obj["runs"]:
            if r["profile"] == profile and r["fanout_cap"] == cap:
                return to_record(r)
        return None
    if isinstance(baseline_obj, list):
        for r in baseline_obj:
            r = to_record(r) if r.get("kind") == "baseline-entry" else r
            if r["profile"] == profile and r["fanout_cap"] == cap:
                return to_record(r) if r.get("kind") == "baseline-entry" else r
        return None
    # single record
    return baseline_obj


def _mem_mb(rec):
    """Normalize peak RSS to a flat number."""
    if "peak_rss_mb" in rec:
        return rec["peak_rss_mb"]
    if "mem" in rec and isinstance(rec["mem"], dict) and "peak_rss_mb" in rec["mem"]:
        return rec["mem"]["peak_rss_mb"]
    return 0.0


def compare_record(baseline: dict, candidate: dict, regress_pct: float, mem_regress_pct: float) -> bool:
    b_rate = baseline["urls_per_s"]
    c_rate = candidate["urls_per_s"]
    if b_rate == 0:
        rate_ok = True
        rate_delta = 0.0
    else:
        rate_delta = (c_rate - b_rate) / b_rate * 100
        rate_ok = rate_delta >= -regress_pct

    b_mem = _mem_mb(baseline)
    c_mem = _mem_mb(candidate)
    if b_mem == 0:
        mem_ok = True
        mem_delta = 0.0
    else:
        mem_delta = (c_mem - b_mem) / b_mem * 100
        mem_ok = mem_delta <= mem_regress_pct

    profile = candidate.get("profile", "?")
    cap = candidate.get("fanout_cap", "?")
    print(f"  [{profile:9s} cap={cap:3d}] urls/s: base={b_rate:7.1f} cand={c_rate:7.1f} delta={rate_delta:+6.1f}%")
    print(f"  [{profile:9s} cap={cap:3d}]   rss MB: base={b_mem:6.1f} cand={c_mem:6.1f} delta={mem_delta:+6.1f}%")
    return rate_ok and mem_ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path, nargs="?",
                        help="Single record JSON; omit if candidate is also an aggregator.")
    parser.add_argument("--regress-pct", type=float, default=5.0)
    parser.add_argument("--mem-regress-pct", type=float, default=10.0)
    args = parser.parse_args()

    base = load(args.baseline)
    candidates = []
    if args.candidate:
        candidates.append(load(args.candidate))
    else:
        # candidate is the aggregator itself
        if isinstance(base, dict) and base.get("kind") == "usp-bench-baseline":
            print("baseline and candidate identical; nothing to compare")
            return 0

    all_ok = True
    for cand in candidates:
        profile = cand.get("profile")
        cap = cand.get("fanout_cap")
        if profile is None or cap is None:
            print(f"ERROR: candidate {args.candidate} missing profile/fanout_cap")
            return 2
        baseline_rec = find_baseline(base, profile, cap)
        if baseline_rec is None:
            print(f"  no baseline for profile={profile} cap={cap}; skipping")
            continue
        ok = compare_record(baseline_rec, cand, args.regress_pct, args.mem_regress_pct)
        all_ok = all_ok and ok

    print("PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
