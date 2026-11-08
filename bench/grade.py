"""Grade benchmark answers against facts taken from the code, so token savings
can't come from wrong answers. Each question has required facts; a fact passes if
any of its phrases appears (case-insensitive). An answer is correct if every fact passes.

    python3 bench/grade.py            # all conditions under ~/raindrop-bench/results
"""
import json
import os
import re
from pathlib import Path

RESULTS = Path(os.environ.get("RAINDROP_BENCH_HOME", Path.home() / "raindrop-bench")) / "results"

# Ground truth from api/config.py, raindrop/redact.py, adapters/kiro.py, adapters/_common.py.
RUBRIC = {
    1: {"similarity weight 0.7": ["0.7", "70%"],
        "recency component": ["recency", "recent"],
        "72h half-life": ["72"]},
    2: {"known values": ["known value", "known secret", "exact"],
        "key/value labels": ["key-value", "key=value", "key/value", "label", "assignment", "authorization"],
        "provider formats": ["format", "aws", "github", "jwt", "pem", "provider"],
        "high entropy": ["entropy"],
        "how it spares code": ["uuid", "sha", "hash", "path", "letters and digits", "placeholder",
                               "allowlist", "skip", "ignored", "left alone", "false positive"]},
    3: {"uses hooks": ["hook"],
        "spools events locally": ["spool", "event"],
        "rebuilds exchanges from them": ["exchange", "reconstruct", "assembl", "build", "pair"]},
    4: {"picks the newest": ["newest", "most recent", "latest"],
        "by modification time": ["mtime", "modif", "modified"],
        "inside this repo": ["repo", "cwd", "working directory"]},
}


def grade(text: str, q: int) -> list[str]:
    t = text.lower()
    return [fact for fact, phrases in RUBRIC[q].items() if not any(p in t for p in phrases)]


def main() -> None:
    for cond in sorted(p.name for p in RESULTS.iterdir() if p.is_dir()):
        print(f"== {cond}")
        total = ok = 0
        for f in sorted((RESULTS / cond).glob("q*_r*.json")):
            q = int(re.match(r"q(\d+)_", f.name).group(1))
            text = json.loads(f.read_text()).get("result", "")
            missing = grade(text, q)
            total += 1; ok += not missing
            print(f"  {f.stem:8} {'OK  ' if not missing else 'MISS'} {', '.join(missing)}")
        print(f"  correct: {ok}/{total}")


if __name__ == "__main__":
    main()
