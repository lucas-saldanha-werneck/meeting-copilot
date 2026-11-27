#!/usr/bin/env python3
"""Measure the fact search on your own notes: the old way (the AI searches with tools) against the fast way
(Jev gate, text search, Jev pick, a small-model check when Jev is unsure), on the same questions.

Usage:
  python3 tools/bench_facts.py questions.json [--mode both|tools|fast] [--repeat N]

questions.json: [{"q": "what is the tax ID of ACME?", "expect": "12.345.678"}, ...]. Each question is said by
"Others" in an invented meeting and goes through the same code a data round runs; "expect" is a piece of
text the right answer contains ("" = the right answer is nothing: no fact shown). It reads your config file
(search folders, models, remote machine, TypeSafe key), never changes it, and uses a temporary data folder.
It calls the real AI and Jev (costs a little). Prints one line per run and a table.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import copilot  # noqa: E402


def norm(text: str) -> str:
    return " ".join(copilot.fold(text).split())


def one(mode: str, q: str) -> dict:
    copilot.E = copilot.State()
    e = copilot.E
    e.meeting = {"id": 1, "title": "Bench", "start": ""}
    e.lines = [{"id": 1, "who": "Others", "start": 10.0, "end": 14.0, "text": q}]
    e.last_id = 1
    lines, seen, shown = list(e.lines), 0, []
    msg = copilot.data_message(lines, seen, shown, copilot.meeting_context())
    info: dict = {}
    t0 = time.monotonic()
    cost = None
    if mode == "fast":
        text = copilot.fact_fast(lines, seen, shown, msg, info)
        cost = info.get("cost", 0.0)
        if text is None:
            text, out = copilot.fact_tools(msg)
            cost = (cost or 0.0) + (out.get("total_cost_usd") or 0.0)
            info["stage"] = (info.get("stage") or "jev out") + ", tools"
    else:
        text, out = copilot.fact_tools(msg)
        cost = out.get("total_cost_usd")
    return {"s": time.monotonic() - t0, "text": "" if copilot.is_empty(text) else text, "cost": cost,
            "info": copilot.fact_log(info) if info else ""}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("questions")
    ap.add_argument("--mode", choices=("both", "tools", "fast"), default="both")
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()
    qs = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        cfg = copilot.load_config()
        cfg["MEETING_COPILOT_DATA_DIR"] = tmp
        copilot.S = copilot.Settings(cfg)
        copilot.REMOTE.reset()
        if copilot.S.fact_mode != "fast" and args.mode != "tools":
            raise SystemExit("the fast fact search is off (no TypeSafe key or no search folders): "
                             + "; ".join(copilot.S.notices))
        modes = ("tools", "fast") if args.mode == "both" else (args.mode,)
        rows = []
        for i, item in enumerate(qs, 1):
            for m in modes:
                for _ in range(args.repeat):
                    r = one(m, item["q"])
                    want = item.get("expect", "")
                    r["ok"] = (norm(want) in norm(r["text"])) if want else not r["text"]
                    rows.append((i, m, r))
                    print(f"Q{i} {m:5s} {r['s']:5.1f}s ok={r['ok']} cost="
                          f"{'-' if r['cost'] is None else format(r['cost'], '.5f')} {r['info']}\n"
                          f"     {r['text'][:200]!r}", flush=True)
        copilot.FACT_SPARE.stop()
        print("\n| Q | way | seconds | cost (US$) | right |\n|---|---|---|---|---|")
        for i, m, r in rows:
            print(f"| Q{i} | {m} | {r['s']:.1f} | {'-' if r['cost'] is None else format(r['cost'], '.5f')} | "
                  f"{'yes' if r['ok'] else 'no'} |")


if __name__ == "__main__":
    main()
