#!/usr/bin/env python3
"""Live meeting copilot.

Reads the live transcript that the Muesli app writes while a meeting is recording
(read-only), and shows on a local web page:
  - "Say this" / "Ask this"                 (AI, about every 25 s)
  - Corrections (wrong fact, contradiction,
    vague commitment) + goals covered        (same AI call)
  - Relevant facts from your notes and any
    MCP tools you configure (optional)       (AI with tools, about every 45 s)
  - Speaking-habit warnings                  (no AI, every 5 s)

The AI runs through the `claude -p` command line (Claude Code). Nothing else leaves the machine.
Configuration: environment variables or ~/.config/meeting-copilot/config (see README).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

__version__ = "0.1.0"

HERE = Path(__file__).resolve().parent
PAGE = HERE / "page.html"

COOLDOWN_WARNING_S = 120   # between any two warnings
REPEAT_WARNING_S = 300     # before the same kind of warning shows again
WINDOW_S = 180             # how much of the conversation the AI sees
AI_TIMEOUT_S = 120

# ---------------------------------------------------------------- config


def default_db() -> Path:
    return Path.home() / "Library/Application Support/Muesli/muesli.db"


def default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base) / "meeting-copilot"


def _expand(v: str) -> str:
    return os.path.expanduser(os.path.expandvars(v.strip()))


def load_config(environ=None) -> dict:
    """KEY=VALUE lines from the config file, then MEETING_COPILOT_* environment variables (these win)."""
    environ = os.environ if environ is None else environ
    cfg: dict[str, str] = {}
    path = Path(_expand(environ.get("MEETING_COPILOT_CONFIG", "") or "~/.config/meeting-copilot/config"))
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            cfg[k.strip()] = v
    for k, v in environ.items():
        if k.startswith("MEETING_COPILOT_"):
            cfg[k] = v
    return cfg


class Settings:
    def __init__(self, cfg: dict) -> None:
        g = lambda k, d="": cfg.get("MEETING_COPILOT_" + k, d)  # noqa: E731
        self.db = Path(_expand(g("DB"))) if g("DB") else default_db()
        self.port = int(g("PORT", "4390"))
        self.model = g("MODEL", "sonnet")
        self.data_model = g("DATA_MODEL", self.model)
        self.poll_s = float(g("POLL_S", "5"))
        self.assist_s = float(g("ASSIST_S", "25"))
        self.data_s = float(g("DATA_S", "45"))
        self.data_dir = Path(_expand(g("DATA_DIR"))) if g("DATA_DIR") else default_data_dir()
        self.sessions_dir = Path(_expand(g("SESSIONS_DIR"))) if g("SESSIONS_DIR") else self.data_dir / "sessions"
        self.save_sessions = g("SAVE_SESSIONS", "1").strip().lower() not in ("0", "no", "false", "off")
        self.notices: list[str] = []
        claude = _expand(g("CLAUDE") or "claude")
        self.claude = claude if os.path.isfile(claude) else shutil.which(claude)

        # Optional: folders the AI may search for facts (your notes, a wiki, a docs folder).
        self.search_dirs: list[Path] = []
        for part in g("SEARCH_DIRS").split(os.pathsep):
            if not part.strip():
                continue
            p = Path(_expand(part))
            if p.is_dir():
                self.search_dirs.append(p.resolve())
            else:
                self.notices.append(f"search folder not found, ignored: {p}")

        # Optional: an MCP config file plus an explicit allowlist of (read-only) tools from it.
        self.mcp_config: Path | None = None
        self.mcp_tools: list[str] = []
        mcp = g("MCP_CONFIG")
        if mcp:
            p = Path(_expand(mcp))
            tools = [t.strip() for t in g("MCP_TOOLS").split(",") if t.strip()]
            if not p.is_file():
                self.notices.append(f"MCP config not found, ignored: {p}")
            elif not tools:
                self.notices.append("MEETING_COPILOT_MCP_CONFIG is set but MEETING_COPILOT_MCP_TOOLS is empty: "
                                    "MCP tools are off (list the read-only tools you allow)")
            else:
                self.mcp_config, self.mcp_tools = p.resolve(), tools

        hosts = [h.strip() for h in g("EXTRA_HOSTS").split(",") if h.strip()]
        self.hosts_ok = {f"127.0.0.1:{self.port}", f"localhost:{self.port}", *hosts}
        self.origins_ok = {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}",
                           *(f"https://{h}" for h in hosts)}

    @property
    def data_enabled(self) -> bool:
        return bool(self.search_dirs or self.mcp_config)


S: Settings = Settings({})

# ---------------------------------------------------------------- prompts

PROMPT_ASSIST = """You are a live meeting copilot for the user.
In the transcript, "You" = the user (microphone). "Others" = the other people (computer audio).
Help ONLY the user. The transcript is automatic: it can have wrong words and missing punctuation.

Return:
- say: ready-made sentences the user can say now (answer something, summarize, steer back to the goal,
  record a decision, acknowledge someone's commitment).
- ask: questions the user can ask (clarify, pin down a vague claim, go deeper, find owner and deadline).
- corrections: something said by ANYONE that looks wrong, contradicts something said before, has an
  inconsistent number, or is a commitment with no owner or no deadline. For each: the quote, the problem
  in 1 sentence, and what the user can say. Do not correct errors of the automatic transcription.
- goals_covered: the numbers of the goals (list below, starting at 0) that the conversation has ALREADY
  covered, looking at the WHOLE transcript (BEFORE + NEW). Covered = the topic was handled and answered,
  even briefly.

Rules:
- 0 to 2 items per list. Silence is normal: empty list when the user does not need help.
- Each item in say, ask and corrections ties to something in the NEW part. BEFORE is only context.
- Short sentences (max. 20 words), first person, in the language the meeting is using.
- Never repeat or rephrase an item from the ALREADY SHOWN list.
"""

PROMPT_DATA = """You are a silent fact assistant in the user's live meeting.
"You" = the user. "Others" = the other people. You can:
{sources}

Act only when the NEW part of the conversation creates a need for information you can find:
- a direct request ("what price did we agree on?", "when did we talk to them?");
- an implicit one ("I think the contract ends next month" -> check the date).

Output:
- Found it: short markdown, at most 6 lines. Start with the fact. End with the source (file or conversation).
  If the fact is internal and should not be said out loud, say so on the first line.
- Nothing to look up, or nothing useful found: answer exactly EMPTY.
- Never invent. Never repeat a fact from the ALREADY SHOWN list.
- At most 4 searches. Be fast: the meeting is happening now.
"""

SCHEMA_ASSIST = {
    "type": "object",
    "properties": {
        "say": {"type": "array", "items": {"type": "string"}},
        "ask": {"type": "array", "items": {"type": "string"}},
        "corrections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "quote": {"type": "string"},
                    "problem": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
                "required": ["quote", "problem", "suggestion"],
            },
        },
        "goals_covered": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["say", "ask", "corrections", "goals_covered"],
}


def data_sources_text() -> str:
    lines = []
    if S.search_dirs:
        lines.append("- search the user's notes in these folders (use Grep and Glob before Read): "
                     + ", ".join(str(d) for d in S.search_dirs) + ";")
    if S.mcp_tools:
        lines.append("- use these read-only tools: " + ", ".join(S.mcp_tools) + ".")
    return "\n".join(lines)

# ---------------------------------------------------------------- state


def now() -> float:
    return time.time()


def clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset(None)

    def reset(self, meeting: dict | None) -> None:
        self.meeting = meeting            # {id, title, start}
        self.lines: list[dict] = []       # {id, who, start, end, text}
        self.last_id = 0
        self.goal = ""
        self.goals: list[dict] = []       # {text, covered}
        self.say: list[dict] = []         # {text, ts}
        self.ask: list[dict] = []
        self.corrections: list[dict] = []  # {quote, problem, suggestion, ts}
        self.data: list[dict] = []        # {markdown, ts}
        self.warnings: list[dict] = []    # {kind, text, ts}
        self.metrics: dict = {}
        self.status = "waiting for a meeting in Muesli"
        self.errors: list[str] = []
        self.last_assist = 0.0
        self.last_data = 0.0
        self.assist_seen = 0              # last line the suggestions AI has seen
        self.data_seen = 0
        self.last_warning = 0.0
        self.muted: set[str] = set()

    def public(self) -> dict:
        with self.lock:
            return {
                "meeting": self.meeting,
                "live": self.meeting is not None,
                "status": self.status,
                "goal": self.goal,
                "goals": self.goals,
                "say": self.say[-12:],
                "ask": self.ask[-12:],
                "corrections": self.corrections[-10:],
                "data": self.data[-10:],
                "data_enabled": S.data_enabled,
                "warnings": self.warnings[-6:],
                "metrics": self.metrics,
                "lines": self.lines[-8:],
                "errors": STARTUP_ERRORS + self.errors[-3:],
                "now": clock(now()),
            }


E = State()
STARTUP_ERRORS: list[str] = []   # setup problems: shown on the page for the whole run


def error(msg: str) -> None:
    line = f"{clock(now())} {msg}"
    print(line, flush=True)
    with E.lock:
        E.errors.append(line)

# ---------------------------------------------------------------- Muesli


def open_db(path: Path) -> sqlite3.Connection:
    # mode=ro: we never write to Muesli's database
    con = sqlite3.connect(f"file:{quote(path.as_posix(), safe='/:')}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def live_meeting(con: sqlite3.Connection) -> dict | None:
    r = con.execute(
        "SELECT id, title, start_time FROM meetings "
        "WHERE meeting_status = 'recording' AND deleted_at IS NULL "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not r:
        return None
    return {"id": r["id"], "title": r["title"], "start": r["start_time"]}


def new_lines(con: sqlite3.Connection, meeting_id: int, after: int) -> list[dict]:
    rows = con.execute(
        "SELECT id, speaker, start_seconds, end_seconds, text FROM meeting_transcript_checkpoints "
        "WHERE meeting_id = ? AND id > ? ORDER BY id",
        (meeting_id, after),
    ).fetchall()
    return [
        {"id": r["id"], "who": r["speaker"], "start": r["start_seconds"],
         "end": r["end_seconds"], "text": r["text"].strip()}
        for r in rows if r["text"].strip()
    ]

# ---------------------------------------------------------------- metrics (no AI)


def compute_metrics(lines: list[dict]) -> dict:
    if not lines:
        return {}
    end = max(f["end"] for f in lines)

    def dur(fs):
        return sum(max(0.0, f["end"] - f["start"]) for f in fs)

    me = [f for f in lines if f["who"] == "You"]
    total = dur(lines) or 1.0
    recent = [f for f in lines if f["end"] >= end - 300]
    me_recent = [f for f in recent if f["who"] == "You"]
    total_recent = dur(recent) or 1.0

    # monologue: the user's consecutive lines, nobody else in between
    monologue = 0.0
    for f in sorted(lines, key=lambda x: x["start"], reverse=True):
        if f["who"] != "You":
            break
        monologue = end - f["start"]

    words_me = sum(len(f["text"].split()) for f in me)
    min_me = dur(me) / 60 or 1.0
    questions = sum(1 for f in me if "?" in f["text"])
    return {
        "minutes": round(end / 60, 1),
        "you_pct": round(100 * dur(me) / total),
        "you_pct_5min": round(100 * dur(me_recent) / total_recent),
        "monologue_s": round(monologue),
        "wpm": round(words_me / min_me) if me else 0,
        "questions": questions,
    }


def check_warnings(m: dict, recent_kinds: set[str] | None = None, muted: set[str] | None = None) -> dict | None:
    """Tests every rule in order and returns the first one that is not muted or shown recently."""
    if not m:
        return None
    if recent_kinds is None:
        recent_kinds = {a["kind"] for a in E.warnings if now() - a["ts"] < REPEAT_WARNING_S}
    muted = E.muted if muted is None else muted
    rules = []
    if m["monologue_s"] >= 90:
        rules.append(("monologue", f"You have been talking for {m['monologue_s']} s. Make room: ask a question."))
    if m["minutes"] >= 2 and m["you_pct_5min"] >= 70:
        rules.append(("talks_too_much", f"You spoke {m['you_pct_5min']}% of the last 5 min. Listen more."))
    if m["minutes"] >= 4 and m["questions"] < m["minutes"] / 4:
        rules.append(("few_questions", f"Only {m['questions']} question(s) in {m['minutes']} min. Ask more."))
    if m["wpm"] > 190:
        rules.append(("fast", f"Pace of {m['wpm']} words/min. Slow down."))
    for kind, text in rules:
        if kind not in muted and kind not in recent_kinds:
            return {"kind": kind, "text": text}
    return None

# ---------------------------------------------------------------- AI


def run_claude(system_prompt: str, message: str, model: str, *, schema: dict | None = None,
               tools: list[str] | None = None, mcp_tools: list[str] | None = None,
               dirs: list[Path] | None = None, max_turns: int = 2) -> dict:
    if not S.claude:
        raise RuntimeError("claude command not found (install Claude Code or set MEETING_COPILOT_CLAUDE)")
    cmd = [
        S.claude, "-p", "--model", model,
        "--no-session-persistence", "--setting-sources", "project",
        "--output-format", "json", "--max-turns", str(max_turns),
        "--append-system-prompt", system_prompt,
        "--strict-mcp-config",
    ]
    tools, mcp_tools, dirs = tools or [], mcp_tools or [], dirs or []
    if tools or mcp_tools:
        if mcp_tools and S.mcp_config:
            cmd += ["--mcp-config", str(S.mcp_config)]
        cmd += ["--tools", ",".join(tools), "--allowedTools", ",".join(tools + mcp_tools)]
        for d in dirs[1:]:
            cmd += ["--add-dir", str(d)]
    else:
        cmd += ["--tools", ""]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    cwd = dirs[0] if dirs else S.data_dir
    cwd.mkdir(parents=True, exist_ok=True)
    # The message goes through stdin: --allowedTools, --mcp-config and --add-dir take several
    # values and would swallow the message if it were the last argument.
    p = subprocess.run(cmd, input=message, capture_output=True, text=True, encoding="utf-8",
                       timeout=AI_TIMEOUT_S, cwd=str(cwd))
    if p.returncode != 0 and not p.stdout.strip():
        raise RuntimeError(f"claude exited {p.returncode}: {p.stderr.strip()[:300]}")
    return json.loads(p.stdout)


def transcript(lines: list[dict], seen_until: int) -> str:
    if not lines:
        return ""
    end = max(f["end"] for f in lines)
    window = [f for f in lines if f["end"] >= end - WINDOW_S]
    before = [f for f in window if f["id"] <= seen_until]
    new = [f for f in window if f["id"] > seen_until]

    def fmt(fs):
        return "\n".join(f"[{int(f['start'] // 60):02d}:{int(f['start'] % 60):02d}] {f['who']}: {f['text']}"
                         for f in fs)

    parts = []
    if before:
        parts.append("## BEFORE (context)\n" + fmt(before))
    parts.append("## NEW\n" + (fmt(new) or "(nothing new)"))
    return "\n\n".join(parts)


def meeting_context() -> str:
    lines = [f"Now: {datetime.now().strftime('%Y-%m-%d %H:%M')}"]  # every round, so the time never freezes
    if E.meeting:
        lines.append(f"Meeting: {E.meeting['title']}")
    if E.goal.strip():
        lines.append(f"User's objective: {E.goal.strip()}")
    if E.goals:
        lines.append("Goals:\n" + "\n".join(
            f"{i}. {m['text']}{' (already covered)' if m['covered'] else ''}" for i, m in enumerate(E.goals)))
    return "\n".join(lines)


def similar(a: str, b: str) -> bool:
    a, b = a.lower().strip(), b.lower().strip()
    return a == b or SequenceMatcher(None, a, b).ratio() > 0.72


def fresh(items: list[str], shown: list[dict]) -> list[str]:
    out = []
    for it in items:
        it = str(it).strip()
        if it and not any(similar(it, j["text"]) for j in shown) and not any(similar(it, s) for s in out):
            out.append(it)
    return out[:2]


def assist_round() -> None:
    with E.lock:
        lines = list(E.lines)
        seen = E.assist_seen
        shown = [d["text"] for d in E.say[-15:] + E.ask[-15:]] + [c["quote"] for c in E.corrections[-10:]]
        ctx = meeting_context()
    msg = (f"{ctx}\n\n## ALREADY SHOWN\n" + ("\n".join(f"- {j}" for j in shown) or "(nothing)")
           + "\n\n" + transcript(lines, seen))
    t0 = now()
    out = run_claude(PROMPT_ASSIST, msg, S.model, schema=SCHEMA_ASSIST)
    r = out.get("structured_output")
    if isinstance(r, str):
        r = json.loads(r)
    if not isinstance(r, dict):
        raise RuntimeError(f"no structured_output: {str(out.get('result'))[:200]}")
    ts = now()
    with E.lock:
        E.assist_seen = max((f["id"] for f in lines), default=seen)
        for t in fresh(r.get("say", []), E.say):
            E.say.append({"text": t, "ts": ts})
        for t in fresh(r.get("ask", []), E.ask):
            E.ask.append({"text": t, "ts": ts})
        for c in r.get("corrections", [])[:2]:
            if isinstance(c, dict) and c.get("quote") and not any(similar(c["quote"], x["quote"]) for x in E.corrections):
                E.corrections.append({"quote": c["quote"], "problem": c.get("problem", ""),
                                      "suggestion": c.get("suggestion", ""), "ts": ts})
        for i in r.get("goals_covered", []):
            if isinstance(i, int) and 0 <= i < len(E.goals):
                E.goals[i]["covered"] = True
    print(f"{clock(ts)} assist ok in {ts - t0:.1f}s", flush=True)


def is_empty(text: str) -> bool:
    return not text or text.strip(" .*`\n").upper().startswith("EMPTY")


def data_round() -> None:
    with E.lock:
        lines = list(E.lines)
        seen = E.data_seen
        shown = [d["markdown"][:160] for d in E.data[-8:]]
        ctx = meeting_context()
    msg = (f"{ctx}\n\n## ALREADY SHOWN\n" + ("\n".join(f"- {j}" for j in shown) or "(nothing)")
           + "\n\n" + transcript(lines, seen))
    t0 = now()
    tools = ["Read", "Grep", "Glob"] if S.search_dirs else []
    out = run_claude(PROMPT_DATA.format(sources=data_sources_text()), msg, S.data_model,
                     tools=tools, mcp_tools=S.mcp_tools if S.mcp_config else [],
                     dirs=S.search_dirs, max_turns=8)
    text = (out.get("result") or "").strip()
    if out.get("subtype") == "error_max_turns" and not text:
        error("data: the AI used up its searches without answering")
    ts = now()
    with E.lock:
        E.data_seen = max((f["id"] for f in lines), default=seen)
        if not is_empty(text):
            E.data.append({"markdown": text, "ts": ts})
    print(f"{clock(ts)} data ok in {ts - t0:.1f}s ({'empty' if is_empty(text) else 'found'})", flush=True)

# ---------------------------------------------------------------- main loop


def save_session() -> None:
    if not E.meeting or not S.save_sessions:
        return
    S.sessions_dir.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now().strftime('%Y-%m-%d')}-meeting-{E.meeting['id']}.json"
    state = E.public()
    with E.lock:
        state["lines"] = list(E.lines)
    path = S.sessions_dir / name
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        os.chmod(path, 0o600)  # transcripts are private
    except OSError:
        pass
    print(f"session saved to {path}", flush=True)


class Queue:
    """One AI call at a time, to keep memory and CPU use low during the call."""

    def __init__(self) -> None:
        self.busy = False

    def try_run(self, task) -> None:
        if self.busy:
            return
        self.busy = True

        def run():
            try:
                task()
            except Exception as ex:
                error(f"{task.__name__}: {ex}")
                traceback.print_exc()
            finally:
                self.busy = False

        threading.Thread(target=run, daemon=True).start()


def loop(db: Path) -> None:
    q = Queue()
    while True:
        try:
            con = open_db(db)
            try:
                live = live_meeting(con)
                current = E.meeting
                if live and (not current or live["id"] != current["id"]):
                    if current:
                        save_session()
                    with E.lock:
                        goal, goals = E.goal, [{**m, "covered": False} for m in E.goals]
                        E.reset(live)
                        E.goal, E.goals = goal, goals  # keep what you typed before the meeting started
                        E.status = "live"
                    print(f"meeting {live['id']} '{live['title']}' started", flush=True)
                elif not live and current:
                    save_session()
                    with E.lock:
                        E.status = f"meeting {current['id']} ended · waiting for the next one"
                        E.meeting = None
                    print(f"meeting {current['id']} ended", flush=True)
                if live:
                    lines = new_lines(con, live["id"], E.last_id)
                    if lines:
                        with E.lock:
                            E.lines.extend(lines)
                            E.last_id = lines[-1]["id"]
                            E.metrics = compute_metrics(E.lines)
            finally:
                con.close()
        except Exception as ex:  # database busy, WAL, etc.: try again next time
            error(f"reading Muesli: {ex}")

        if E.meeting:
            w = check_warnings(E.metrics)
            if w and now() - E.last_warning >= COOLDOWN_WARNING_S:
                with E.lock:
                    E.warnings.append({**w, "ts": now()})
                    E.last_warning = now()

            if S.claude and not q.busy:
                if E.last_id > E.assist_seen and now() - E.last_assist >= S.assist_s:
                    E.last_assist = now()
                    q.try_run(assist_round)
                elif S.data_enabled and E.last_id > E.data_seen and now() - E.last_data >= S.data_s:
                    E.last_data = now()
                    q.try_run(data_round)
        time.sleep(S.poll_s)

# ---------------------------------------------------------------- local server


class Page(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _host_ok(self) -> bool:
        # blocks DNS rebinding: only our own host names are answered
        return self.headers.get("Host", "") in S.hosts_ok

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"host", "text/plain")
        if self.path == "/":
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if self.path == "/state":
            return self._send(200, json.dumps(E.public(), ensure_ascii=False).encode(), "application/json")
        self._send(404, b"", "text/plain")

    def do_POST(self):
        # no CORS headers: another site can neither read this nor send JSON here
        if not self._host_ok() or self.headers.get("Origin") not in S.origins_ok \
                or not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._send(403, b"origin", "text/plain")
        n = int(self.headers.get("Content-Length", "0") or 0)
        try:
            body = json.loads(self.rfile.read(min(n, 100_000)) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, b"json", "text/plain")
        if not isinstance(body, dict):
            return self._send(400, b"json", "text/plain")
        with E.lock:
            if self.path == "/context":
                E.goal = str(body.get("goal", ""))[:2000]
                old = {m["text"]: m["covered"] for m in E.goals}
                E.goals = [{"text": t, "covered": old.get(t, False)}
                           for t in (str(x).strip() for x in body.get("goals", []) or []) if t][:12]
            elif self.path == "/mute":
                E.muted.add(str(body.get("kind", "")))
            else:
                return self._send(404, b"", "text/plain")
        self._send(200, b'{"ok":true}', "application/json")


def _stop(*_):
    raise KeyboardInterrupt


def main(argv=None) -> None:
    global S
    ap = argparse.ArgumentParser(description="Live meeting copilot (reads the Muesli app's live transcript).")
    ap.add_argument("--db", type=Path, help="Muesli database (opened read-only)")
    ap.add_argument("--port", type=int, help="local port (default 4390)")
    ap.add_argument("--version", action="version", version=f"meeting-copilot {__version__}")
    args = ap.parse_args(argv)
    cfg = load_config()
    if args.db:
        cfg["MEETING_COPILOT_DB"] = str(args.db)
    if args.port:
        cfg["MEETING_COPILOT_PORT"] = str(args.port)
    S = Settings(cfg)
    if not S.db.exists():
        raise SystemExit(f"Muesli database not found: {S.db}\n"
                         "Install and run Muesli, or point MEETING_COPILOT_DB / --db at its muesli.db.")
    for n in S.notices:
        print(f"note: {n}", flush=True)
    if not S.claude:
        msg = ("claude command not found: only the speaking metrics and warnings will work "
               "(install Claude Code or set MEETING_COPILOT_CLAUDE)")
        STARTUP_ERRORS.append(msg)
        print(msg, flush=True)
    if not S.data_enabled:
        print("note: fact search is off (set MEETING_COPILOT_SEARCH_DIRS and/or MEETING_COPILOT_MCP_CONFIG)",
              flush=True)
    # `meeting-copilot stop` sends SIGTERM: turn it into a normal exit so the session is saved
    signal.signal(signal.SIGTERM, _stop)
    threading.Thread(target=loop, args=(S.db,), daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", S.port), Page)
    print(f"meeting-copilot on http://127.0.0.1:{S.port}  (database: {S.db})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        save_session()
        sys.exit(0)


if __name__ == "__main__":
    main()
