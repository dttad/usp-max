"""Aggregate per-profile/per-cap baselines into a single baseline.json.

Reads bench/results/baseline-*.json and writes bench/results/baseline.json
with a summary suitable for `bench/compare.py` to compare against.

Usage:
    uv run bench/aggregate.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

RESULTS_DIR = Path(__file__).parent / "results"


def main() -> int:
    runs = {}
    for path in sorted(RESULTS_DIR.glob("baseline-*.json")):
        with path.open() as f:
            rec = json.load(f)
        # Skip duplicate runs (we run each baseline twice for noise estimate)
        key = (rec["profile"], rec["fanout_cap"])
        runs.setdefault(key, []).append(rec)

    # Pick the median run per (profile, cap)
    aggregated = []
    for (profile, cap), recs in sorted(runs.items()):
        recs.sort(key=lambda r: r["urls_per_s"])
        median = recs[len(recs) // 2]
        aggregated.append({
            "profile": profile,
            "fanout_cap": cap,
            "n_runs": len(recs),
            "wall_s_min": min(r["wall_s"] for r in recs),
            "wall_s_max": max(r["wall_s"] for r in recs),
            "urls_per_s_median": median["urls_per_s"],
            "peak_rss_mb_median": median["mem"]["peak_rss_mb"],
            "urls_total": median["urls_total"],
        })

    out = {
        "kind": "usp-bench-baseline",
        "version": 1,
        "runs": aggregated,
    }

    target = RESULTS_DIR / "baseline.json"
    with target.open("w") as f:
        json.dump(out, f, indent=2)
    print(f"wrote {target}")
    for r in aggregated:
        print(f"  profile={r['profile']:9s} cap={r['fanout_cap']:3d} "
              f"urls/s={r['urls_per_s_median']:7.1f} peak={r['peak_rss_mb_median']:6.1f}MB "
              f"runs={r['n_runs']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
