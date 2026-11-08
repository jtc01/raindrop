"""Summarize ~/raindrop-bench/results/{baseline,raindrop}/*.json into a comparison table.

Tokens = input + cache_creation + cache_read + output (everything the model
processed). Cost comes from Claude Code's own total_cost_usd, which accounts for
cheaper cache reads, so it's the fairer money comparison.
"""
import json
import re
import statistics
import sys
from pathlib import Path

import os
ROOT = Path(os.environ.get("RAINDROP_BENCH_HOME", Path.home() / "raindrop-bench")) / "results"


def load(cond):
    runs = {}
    for f in sorted((ROOT / cond).glob("q*_r*.json")):
        q = int(re.match(r"q(\d+)_", f.name).group(1))
        try:
            d = json.loads(f.read_text())
        except Exception:
            print(f"skipping unreadable {f}", file=sys.stderr)
            continue
        if d.get("is_error"):
            print(f"skipping errored run {f}", file=sys.stderr)
            continue
        u = d.get("usage", {})
        tokens = sum(u.get(k, 0) or 0 for k in
                     ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
        runs.setdefault(q, []).append({"tokens": tokens, "cost": d.get("total_cost_usd") or 0.0,
                                       "turns": d.get("num_turns") or 0, "secs": (d.get("duration_ms") or 0) / 1000})
    return runs


def med(rows, key):
    return statistics.median(r[key] for r in rows) if rows else None


base, rd = load("baseline"), load("raindrop")
qs = sorted(set(base) | set(rd))
if not qs:
    sys.exit("No results yet. Run bench/run.sh baseline and bench/run.sh raindrop first.")

print(f"{'Q':<4}{'baseline tok':>14}{'raindrop tok':>14}{'saved':>8}{'base $':>9}{'rd $':>9}{'base turns':>12}{'rd turns':>10}")
tb = tr = cb = cr = 0
for q in qs:
    b, r = base.get(q, []), rd.get(q, [])
    bt, rt = med(b, "tokens"), med(r, "tokens")
    saved = f"{(1 - rt / bt) * 100:.0f}%" if bt and rt is not None else "-"
    print(f"q{q:<3}{bt or 0:>14,.0f}{rt or 0:>14,.0f}{saved:>8}{med(b,'cost') or 0:>9.3f}{med(r,'cost') or 0:>9.3f}"
          f"{med(b,'turns') or 0:>12.0f}{med(r,'turns') or 0:>10.0f}")
    if bt and rt is not None:
        tb += bt; tr += rt; cb += med(b, "cost"); cr += med(r, "cost")
if tb:
    print(f"\nTotal (median per question): {tb:,.0f} -> {tr:,.0f} tokens, {(1 - tr / tb) * 100:.0f}% fewer; "
          f"${cb:.3f} -> ${cr:.3f}, {(1 - cr / cb) * 100 if cb else 0:.0f}% cheaper")
print("Runs per question:", {q: (len(base.get(q, [])), len(rd.get(q, []))) for q in qs})
