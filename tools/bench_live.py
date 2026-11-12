#!/usr/bin/env python3
"""Measure suggestions rounds through the real code path (assist_round) against an invented meeting.

Usage:
  python3 tools/bench_live.py [--rounds N] [--mode both|spawn|kept] [--claude PATH] [--model sonnet]
                              [--language "Brazilian Portuguese"] [--credit]

Each round adds one transcript line and runs one suggestions round, like a live meeting:
  spawn  a new `claude -p` per round (MEETING_COPILOT_PERSISTENT_AI=0)
  kept   the kept-open process, warmed up first as the "Turn on AI" click does
It prints each round and p50 / p90 / max per mode. It calls the real claude (costs a little), never
reads your config file or your data folder (a temporary folder is used), and never touches Muesli.
--credit uses your claude-credito setup (API credit first) for the kept-open process; default: off.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import copilot  # noqa: E402
import fake_meeting  # noqa: E402

EXTRA = [
    ("Others", 6, "We also need to talk about the garden, they quoted eight hundred a month."),
    ("You", 4, "Eight hundred on top of the fifteen percent?"),
    ("Others", 5, "Yes, and the window cleaning would be another three hundred."),
    ("You", 6, "So we would go from four thousand to almost five thousand six hundred."),
    ("Others", 4, "I think it is closer to five thousand, but I need to check."),
    ("You", 5, "Who can send us the final numbers, and by when?"),
    ("Others", 3, "Probably next week."),
]


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]


def run(mode: str, n: int, args, tmp: str) -> list[float]:
    cfg = {"MEETING_COPILOT_DATA_DIR": tmp, "MEETING_COPILOT_MODEL": args.model,
           "MEETING_COPILOT_PERSISTENT_AI": "1" if mode == "kept" else "0",
           "MEETING_COPILOT_API_CREDIT": "on" if args.credit and mode == "kept" else "off"}
    if args.claude:
        cfg["MEETING_COPILOT_CLAUDE"] = args.claude
    if args.language:
        cfg["MEETING_COPILOT_LANGUAGE"] = args.language
    copilot.S = copilot.Settings(cfg)
    if not copilot.S.claude:
        raise SystemExit("claude not found (use --claude)")
    copilot.E = copilot.State()
    copilot.SESSION = copilot.AssistSession()
    e = copilot.E
    e.meeting = {"id": 1, "title": fake_meeting.TITLE, "start": ""}
    e.goal, e.goals = "Keep the monthly price flat", [{"text": "Price", "covered": False},
                                                       {"text": "Contract end date", "covered": False}]
    script = fake_meeting.SCRIPT + EXTRA
    t, lid = 0.0, 0

    def add(k: int) -> None:
        nonlocal t, lid
        who, dur, text = script[k % len(script)]
        lid += 1
        e.lines.append({"id": lid, "who": who, "start": t, "end": t + dur, "text": text})
        e.last_id = lid
        t += dur + 1

    for k in range(4):     # some conversation before the AI is turned on
        add(k)
    if mode == "kept":
        copilot.SESSION.warm(1, "bench")
        time.sleep(5)      # as after the click: the process is up before the first line comes
    out = []
    for i in range(n):
        add(4 + i)
        t0 = time.monotonic()
        try:
            copilot.assist_round()
            ok = True
        except Exception as ex:   # noqa: BLE001
            ok = False
            print(f"  {mode} round {i + 1}: failed: {ex}", flush=True)
        dt = time.monotonic() - t0
        r = e.rounds[-1] if e.rounds else {}
        print(f"  {mode} round {i + 1}: {dt:.2f}s ok={ok} first_byte={r.get('first_byte', '-')} "
              f"turn={r.get('turn', '-')} attempts={r.get('attempts', 1)} say={len(e.say)}", flush=True)
        if ok:
            out.append(dt)
    copilot.SESSION.close("bench done", wait=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--mode", choices=("both", "spawn", "kept"), default="both")
    ap.add_argument("--claude", default=os.environ.get("MEETING_COPILOT_CLAUDE", ""))
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--language", default="")
    ap.add_argument("--credit", action="store_true")
    args = ap.parse_args()
    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        modes = ("spawn", "kept") if args.mode == "both" else (args.mode,)
        for m in modes:
            print(f"{m}: {args.rounds} rounds", flush=True)
            results[m] = run(m, args.rounds, args, tmp)
    print(f"\nload average: {' '.join(f'{x:.1f}' for x in os.getloadavg())}")
    for m, xs in results.items():
        if xs:
            print(f"{m:6s} n={len(xs):2d}  p50={pct(xs, .5):.2f}s  p90={pct(xs, .9):.2f}s  max={max(xs):.2f}s")
        else:
            print(f"{m:6s} no successful round")


if __name__ == "__main__":
    main()
