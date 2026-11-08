#!/usr/bin/env python3
"""Simulate a live Muesli meeting in a separate SQLite file, to try meeting-copilot without a real call.

Usage:
  python3 tools/fake_meeting.py prepare <test.db> [--from <muesli.db>]
        create the test database (an empty Muesli-like schema, or a read-only copy of a real
        Muesli database with --from) and start a meeting with status 'recording'
  python3 tools/fake_meeting.py speak <test.db> [seconds]   insert one line every [seconds] s (default 4)
  python3 tools/fake_meeting.py end <test.db>               mark the meeting as completed

Then run: meeting-copilot --db <test.db>
It never writes to the real Muesli database: it refuses that path, and --from is opened read-only.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REAL = Path.home() / "Library/Application Support/Muesli/muesli.db"
TITLE = "TEST meeting-copilot - supplier renewal"

# Only the tables and columns meeting-copilot reads (Muesli's real tables have more columns).
SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    start_time TEXT NOT NULL,
    meeting_status TEXT NOT NULL DEFAULT 'completed',
    deleted_at REAL
);
CREATE TABLE IF NOT EXISTS meeting_transcript_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    timestamp_label TEXT NOT NULL,
    speaker TEXT NOT NULL,
    start_seconds REAL NOT NULL,
    end_seconds REAL NOT NULL,
    text TEXT NOT NULL
);
"""

# (speaker, seconds speaking, text) - an invented conversation.
SCRIPT = [
    ("Others", 6, "Thanks for joining. We wanted to go over the renewal of the cleaning contract."),
    ("You", 5, "Sure. Today we pay four thousand a month for the three buildings."),
    ("Others", 9, "Right, and the new proposal adds fifteen percent, which is a lot for us."),
    ("You", 4, "Let me check that number."),
    ("Others", 8, "And the old contract ends in March, correct?"),
    ("You", 40, "So the way it works is that the supplier sends the team, they bring the products, "
                "they handle the schedule, they replace people on vacation, and we only pay the monthly fee, "
                "and the idea was always to keep it simple so nobody here has to manage it"),
    ("You", 35, "and there is also the question of the windows, which is billed apart, and the garden, "
                "which they said they could include, and we might grow to a fourth building next year"),
    ("Others", 7, "OK. We can look at that later with the board, someone will sort it out."),
    ("You", 3, "Fine."),
    ("Others", 6, "One more thing: is the window cleaning included in the monthly fee too?"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def prepare(target: Path, source: Path | None = None) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    dest = sqlite3.connect(target)
    if source:
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        src.backup(dest)
        src.close()
    else:
        dest.executescript(SCHEMA)
    dest.execute("INSERT INTO meetings (title, start_time, meeting_status) VALUES (?, ?, 'recording')",
                 (TITLE, now_iso()))
    dest.commit()
    mid = dest.execute("SELECT max(id) FROM meetings").fetchone()[0]
    dest.close()
    print(f"test database ready: {target} · test meeting id={mid}")
    return mid


def meeting_id(con: sqlite3.Connection) -> int:
    return con.execute("SELECT id FROM meetings WHERE title = ? ORDER BY id DESC LIMIT 1", (TITLE,)).fetchone()[0]


def speak(target: Path, interval: float, script=SCRIPT) -> None:
    con = sqlite3.connect(target)
    mid = meeting_id(con)
    t = 0.0
    for who, dur, text in script:
        con.execute(
            "INSERT INTO meeting_transcript_checkpoints "
            "(meeting_id, timestamp_label, speaker, start_seconds, end_seconds, text) VALUES (?,?,?,?,?,?)",
            (mid, f"00:{int(t // 60):02d}:{int(t % 60):02d}", who, t, t + dur, text),
        )
        con.commit()
        print(f"[{t:5.0f}s] {who}: {text[:60]}", flush=True)
        t += dur + 1
        if interval:
            time.sleep(interval)
    con.close()


def end(target: Path) -> None:
    con = sqlite3.connect(target)
    con.execute("UPDATE meetings SET meeting_status = 'completed' WHERE id = ?", (meeting_id(con),))
    con.commit()
    con.close()
    print("test meeting ended")


def main(argv: list[str]) -> None:
    if len(argv) < 2 or argv[0] not in ("prepare", "speak", "end"):
        raise SystemExit(__doc__)
    action, path = argv[0], Path(argv[1]).resolve()
    if REAL.exists() and path == REAL.resolve():
        raise SystemExit("refused: this is the real Muesli database")
    if action == "prepare":
        source = None
        if "--from" in argv:
            source = Path(argv[argv.index("--from") + 1]).resolve()
        prepare(path, source)
    elif action == "speak":
        speak(path, float(argv[2]) if len(argv) > 2 else 4)
    else:
        end(path)


if __name__ == "__main__":
    main(sys.argv[1:])
