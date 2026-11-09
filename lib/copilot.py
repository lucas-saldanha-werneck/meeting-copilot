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
Configuration: environment variables or ~/.config/meeting-copilot/config (see README, "Your settings").
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

__version__ = "0.6.0"

HERE = Path(__file__).resolve().parent
PAGE = HERE / "page.html"
STARTER = HERE.parent / "config.example"   # copied to your config path on first run (seed_config)

COOLDOWN_WARNING_S = 120   # between any two warnings
REPEAT_WARNING_S = 300     # before the same kind of warning shows again
WINDOW_S = 180             # how much of the conversation the AI sees
AI_TIMEOUT_S = 120
NEXT_MEETING_ARM_S = 30 * 60   # a "Turn on AI for the next meeting" click expires after this
CONTEXT_MAX_CHARS = 8000   # of your context file that goes into each prompt
PROMPT_MAX_CHARS = 20000   # of your prompt file (it goes on the claude command line)

# ---------------------------------------------------------------- config


def default_db() -> Path:
    return Path.home() / "Library/Application Support/Muesli/muesli.db"


def default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base) / "meeting-copilot"


def _expand(v: str) -> str:
    return os.path.expanduser(os.path.expandvars(v.strip()))


def config_path(environ=None) -> Path:
    """Your config file: $MEETING_COPILOT_CONFIG, else $XDG_CONFIG_HOME or ~/.config, then meeting-copilot/config."""
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(_expand(environ.get("MEETING_COPILOT_CONFIG", "") or os.path.join(base, "meeting-copilot", "config")))


def seed_config(environ=None) -> None:
    """First run: create your config file from config.example (every line commented out) when missing.

    Never overwrites or changes a file that is there, skips a path that is not absolute, and
    ignores every error: the program then runs on its defaults, as without a file."""
    try:
        path = config_path(environ)
        if not path.is_absolute() or os.path.lexists(path):
            return
        data = STARTER.read_bytes()
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_EXCL: if the file shows up meanwhile, this fails instead of overwriting it.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    except Exception:
        return
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except Exception:
        try:
            os.remove(path)  # the half-written file is ours: remove it so the next run tries again
        except OSError:
            pass


def load_config(environ=None) -> dict:
    """KEY=VALUE lines from the config file, then MEETING_COPILOT_* environment variables (these win)."""
    environ = os.environ if environ is None else environ
    cfg: dict[str, str] = {}
    path = config_path(environ)
    if path.is_file():
        # utf-8-sig: a BOM from an editor is fine; a stray non-UTF-8 byte never stops the program
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            # "one quoted value" loses its quotes; "a" "b" (quoted words inside) is kept as written
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'" and v[0] not in v[1:-1]:
                v = v[1:-1]
            cfg[k.strip()] = v
    for k, v in environ.items():
        if k.startswith("MEETING_COPILOT_"):
            cfg[k] = v
    return cfg


# Placeholders a prompt file may use. The built-in prompts have none for language and context in their
# text: the language line and the context block are added at their end. A prompt file may place them
# instead: {language} becomes the language name (or "the language the meeting is using" when
# MEETING_COPILOT_LANGUAGE is empty), {context} the context block (or nothing). The fact-search prompt also
# has {sources}: the list of folders and tools it may search. A part the file does not place is added at
# the end, as for the built-in.
PROMPT_PLACEHOLDERS = ("{language}", "{context}")
DATA_PROMPT_PLACEHOLDERS = ("{language}", "{context}", "{sources}")
PLACEHOLDER_RE = re.compile("|".join(sorted(map(re.escape, set(PROMPT_PLACEHOLDERS + DATA_PROMPT_PLACEHOLDERS)))))


# What each call's reply must keep so its parser works, per prompt kind:
#  assist: JSON in a fixed shape (--json-schema, SCHEMA_ASSIST), so a prompt file can't break the parser. It
#          can leave the lists empty if it never asks for them: say and ask must stay in it.
#  data:   free text. A reply that starts with EMPTY means "nothing found" (is_empty); a prompt that never
#          names that word makes the AI write "nothing found" in prose, and that prose shows as a fact.
PROMPT_KINDS = {
    "assist": {"name": "prompt file", "placeholders": PROMPT_PLACEHOLDERS, "words": ("say", "ask"), "case": re.I,
               "missing": ("never names the output field(s) {missing}: the suggestions come back in the fields "
                           "say, ask, corrections, goals_covered; without asking for {missing_and} that list "
                           "may stay empty")},
    "data": {"name": "data prompt file", "placeholders": DATA_PROMPT_PLACEHOLDERS, "words": ("EMPTY",), "case": 0,
             "missing": ("never names the word EMPTY: a reply that starts with EMPTY is how the fact search "
                         "says \"nothing found\"; without it, a no-result answer shows up as a fact")},
}


def load_prompt_file(value: str, kind: str, notices: list[str]) -> str:
    """Text of a prompt file setting, or "" (= the built-in prompt) with one note when it can't be used."""
    if not value:
        return ""
    name, holders = PROMPT_KINDS[kind]["name"], PROMPT_KINDS[kind]["placeholders"]
    p = Path(_expand(value))
    try:
        text = p.read_text(encoding="utf-8-sig").strip()
    except OSError as e:
        notices.append(f"{name} not found or unreadable, using the built-in prompt: {p} ({e.__class__.__name__})")
        return ""
    except UnicodeDecodeError:
        notices.append(f"{name} is not UTF-8 text, using the built-in prompt: {p} (save it as UTF-8)")
        return ""
    if not text:
        notices.append(f"{name} is empty, using the built-in prompt: {p}")
        return ""
    if len(text) > PROMPT_MAX_CHARS:
        notices.append(f"{name} has {len(text)} characters; only the first {PROMPT_MAX_CHARS} are used: {p}")
        cut = PROMPT_MAX_CHARS
        brace = text.rfind("{", 0, cut)  # never cut a {placeholder} in half
        if brace != -1 and "}" not in text[brace:cut] and cut - brace <= max(map(len, holders)):
            cut = brace
        text = text[:cut]
    notices.extend(check_prompt(text, p, kind))
    return text


def check_prompt(text: str, path, kind: str = "assist") -> list[str]:
    """Startup notes about a prompt file: words its reply's parser needs that it never names, and {words}
    that are no placeholder."""
    k = PROMPT_KINDS[kind]
    notes = []
    missing = [w for w in k["words"] if not re.search(r"\b" + w + r"\b", text, k["case"])]
    if missing:
        notes.append(f"{k['name']} {path} " + k["missing"].format(missing=", ".join(missing),
                                                                     missing_and=" and ".join(missing)))
    unknown = sorted(set(re.findall(r"\{[A-Za-z_]+\}", text)) - set(k["placeholders"]))
    if unknown:
        notes.append(f"{k['name']} {path}: {', '.join(unknown)} is not a placeholder and stays as written "
                     f"(placeholders: {', '.join(k['placeholders'])})")
    return notes


class Settings:
    def __init__(self, cfg: dict) -> None:
        g = lambda k, d="": cfg.get("MEETING_COPILOT_" + k) or d  # noqa: E731  (KEY= with no value: the default)
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
        # CONFIRM_AI=1: no AI call in a meeting until you click "Turn on AI" on the page
        # (for a copilot that starts at login and should not spend AI on every recording).
        self.confirm_ai = g("CONFIRM_AI", "0").strip().lower() in ("1", "yes", "true", "on")
        self.notices: list[str] = []
        # SAVE_FORMAT=markdown: a readable "<Muesli export name>-copilot.md" instead of the JSON
        self.save_format = g("SAVE_FORMAT", "json").strip().lower()
        if self.save_format not in ("json", "markdown"):
            self.notices.append(f"MEETING_COPILOT_SAVE_FORMAT must be json or markdown, using json: {self.save_format}")
            self.save_format = "json"
        # how long to wait for Muesli's own export (it names the file after the final title)
        try:
            self.muesli_wait_s = float(g("MUESLI_WAIT_S", "600"))
        except ValueError:
            self.notices.append(f"MEETING_COPILOT_MUESLI_WAIT_S is not a number, using 600: {g('MUESLI_WAIT_S')}")
            self.muesli_wait_s = 600.0
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

        # Optional: what the AI should know about you (name, role, projects, where your notes are),
        # and the language every suggestion and fact is written in.
        self.context = ""
        ctx = g("CONTEXT_FILE")
        if ctx:
            p = Path(_expand(ctx))
            try:
                self.context = p.read_text(encoding="utf-8-sig").strip()[:CONTEXT_MAX_CHARS]
            except OSError:
                self.notices.append(f"context file not found or unreadable, ignored: {p}")
            except UnicodeDecodeError:
                self.notices.append(f"context file is not UTF-8 text, ignored: {p}")
        self.language = g("LANGUAGE").strip()

        # Optional: your own text in place of the built-in prompts (PROMPT_ASSIST, PROMPT_DATA).
        # Missing, unreadable, not UTF-8 or empty: one note, and the built-in prompt.
        self.prompt = load_prompt_file(g("PROMPT_FILE"), "assist", self.notices)
        self.data_prompt = load_prompt_file(g("DATA_PROMPT_FILE"), "data", self.notices)

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


def language_text() -> str:
    if not S.language:
        return ""
    return (f"Write every item in {S.language}, whatever language the meeting uses "
            "(keep the word EMPTY as is).")


def context_text() -> str:
    return "## About the user (from their context file)\n" + S.context if S.context else ""


def personalize(prompt: str, sources: str | None = None) -> str:
    """Add your language and context file (MEETING_COPILOT_LANGUAGE, MEETING_COPILOT_CONTEXT_FILE), and for
    the fact search the sources: in place of {language} / {context} / {sources} when the prompt has them,
    else at the end. One pass, so a placeholder inside your context text is never filled again."""
    values = {"language": S.language or "the language the meeting is using", "context": context_text()}
    if sources is not None:
        values["sources"] = sources
    tail = []
    if sources and "{sources}" not in prompt:
        tail.append("You can:\n" + sources)
    if S.language and "{language}" not in prompt:
        tail.append(language_text())
    if S.context and "{context}" not in prompt:
        tail.append(context_text())
    if sources is not None and S.language and "{language}" in prompt:
        # the fact search's "nothing found" word must not be translated (is_empty reads it)
        tail.append("Keep the word EMPTY in English, as is.")
    prompt = PLACEHOLDER_RE.sub(lambda m: values.get(m.group(0)[1:-1], m.group(0)), prompt)
    return prompt + "".join(f"\n{t}\n" for t in tail)


def assist_prompt() -> str:
    return personalize(S.prompt or PROMPT_ASSIST)


def data_prompt() -> str:
    return personalize(S.data_prompt or PROMPT_DATA, sources=data_sources_text())


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
        # CONFIRM_AI: the meeting id the user turned AI on for, or "next" (armed at ai_next_at)
        self.ai_for: int | str | None = None
        self.ai_next_at = 0.0
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

    def ai_allowed(self, meeting_id=None) -> bool:
        """May the AI run for this meeting (default: the current one)? Always yes without CONFIRM_AI."""
        if not S.confirm_ai:
            return True
        mid = meeting_id if meeting_id is not None else (self.meeting or {}).get("id")
        return mid is not None and self.ai_for == mid

    def ai_armed(self) -> bool:
        return self.ai_for == "next" and now() - self.ai_next_at <= NEXT_MEETING_ARM_S

    def public(self, full: bool = False) -> dict:
        """The page state. full=True: every item (for the saved session), not only the latest."""
        def last(items, n):
            return list(items) if full else items[-n:]
        with self.lock:
            return {
                "meeting": self.meeting,
                "live": self.meeting is not None,
                "status": self.status,
                "goal": self.goal,
                "goals": self.goals,
                "say": last(self.say, 12),
                "ask": last(self.ask, 12),
                "corrections": last(self.corrections, 10),
                "data": last(self.data, 10),
                "data_enabled": S.data_enabled,
                "confirm_ai": S.confirm_ai,
                "ai_on": self.meeting is not None and self.ai_allowed(),
                "ai_next": S.confirm_ai and self.ai_armed(),
                "warnings": last(self.warnings, 6),
                "metrics": self.metrics,
                "lines": last(self.lines, 8),
                "errors": STARTUP_ERRORS + last(self.errors, 3),
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
    try:
        p = subprocess.run(cmd, input=message, capture_output=True, text=True, encoding="utf-8",
                           timeout=AI_TIMEOUT_S, cwd=str(cwd))
    except subprocess.TimeoutExpired as ex:
        err = ex.stderr or b""
        err = err.decode("utf-8", "replace") if isinstance(err, bytes) else err
        raise RuntimeError(f"claude did not answer in {AI_TIMEOUT_S}s; its stderr: "
                           f"{err.strip()[-500:] or '(empty)'}") from None
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


def still_wanted(mid) -> bool:
    """Called under E.lock when an AI answer comes back: drop it if AI was stopped or the meeting changed."""
    if E.ai_allowed(mid) and (E.meeting or {}).get("id") == mid:
        return True
    print(f"{clock(now())} AI answer dropped (AI stopped or meeting changed)", flush=True)
    return False


def assist_round() -> None:
    with E.lock:
        mid = (E.meeting or {}).get("id")
        if not E.ai_allowed(mid):
            return
        lines = list(E.lines)
        seen = E.assist_seen
        shown = [d["text"] for d in E.say[-15:] + E.ask[-15:]] + [c["quote"] for c in E.corrections[-10:]]
        ctx = meeting_context()
    msg = (f"{ctx}\n\n## ALREADY SHOWN\n" + ("\n".join(f"- {j}" for j in shown) or "(nothing)")
           + "\n\n" + transcript(lines, seen))
    t0 = now()
    out = run_claude(assist_prompt(), msg, S.model, schema=SCHEMA_ASSIST)
    r = out.get("structured_output")
    if isinstance(r, str):
        r = json.loads(r)
    if not isinstance(r, dict):
        raise RuntimeError(f"no structured_output: {str(out.get('result'))[:200]}")
    ts = now()
    with E.lock:
        if not still_wanted(mid):
            return
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
        mid = (E.meeting or {}).get("id")
        if not E.ai_allowed(mid):
            return
        lines = list(E.lines)
        seen = E.data_seen
        shown = [d["markdown"][:160] for d in E.data[-8:]]
        ctx = meeting_context()
    msg = (f"{ctx}\n\n## ALREADY SHOWN\n" + ("\n".join(f"- {j}" for j in shown) or "(nothing)")
           + "\n\n" + transcript(lines, seen))
    t0 = now()
    tools = ["Read", "Grep", "Glob"] if S.search_dirs else []
    out = run_claude(data_prompt(), msg, S.data_model,
                     tools=tools, mcp_tools=S.mcp_tools if S.mcp_config else [],
                     dirs=S.search_dirs, max_turns=8)
    text = (out.get("result") or "").strip()
    if out.get("subtype") == "error_max_turns" and not text:
        error("data: the AI used up its searches without answering")
    ts = now()
    with E.lock:
        if not still_wanted(mid):
            return
        E.data_seen = max((f["id"] for f in lines), default=seen)
        if not is_empty(text):
            E.data.append({"markdown": text, "ts": ts})
    print(f"{clock(ts)} data ok in {ts - t0:.1f}s ({'empty' if is_empty(text) else 'found'})", flush=True)

# ---------------------------------------------------------------- main loop


def parse_iso(text) -> datetime | None:
    """Muesli's timestamps ('2026-11-09T13:13:13Z') as an aware datetime, or None."""
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def _write_private(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)  # transcripts are private
    except OSError:
        pass


# Markdown sessions are written to disk as soon as a meeting ends, under a name from the current title.
# Muesli often renames the meeting and exports it a minute later; until MUESLI_WAIT_S has passed the file
# waits here and is renamed to "<Muesli export name>-copilot.md" when the export shows up in Muesli's log.
PENDING: list[dict] = []   # {id, ended, snap, path}
PENDING_LOCK = threading.Lock()


def save_session(final: bool = False) -> None:
    """Called when a meeting ends and on exit (final=True)."""
    if E.meeting and S.save_sessions:
        snap = E.public(full=True)
        mid = E.meeting["id"]
        try:
            if S.save_format == "markdown":
                with PENDING_LOCK:
                    if any(it["id"] == mid for it in PENDING):
                        return   # already saved by the other thread
                    path = write_markdown(snap, meeting_row(None, mid), None)
                    if not final:
                        PENDING.append({"id": mid, "ended": now(), "snap": snap, "path": path})
            else:
                S.sessions_dir.mkdir(parents=True, exist_ok=True)
                path = S.sessions_dir / f"{datetime.now().strftime('%Y-%m-%d')}-meeting-{mid}.json"
                _write_private(path, json.dumps(snap, ensure_ascii=False, indent=1))
                print(f"session saved to {path}", flush=True)
        except Exception as ex:   # never lose the loop or the exit over a save
            error(f"saving the session: {ex}")


def muesli_slug(title: str) -> str:
    """The file stem Muesli gives an exported meeting (MeetingExporter.suggestedFilename): runs of
    letters, marks and digits (Swift CharacterSet.alphanumerics) joined by '-', lowercased, 50 chars."""
    parts, cur = [], []
    for ch in title:
        if unicodedata.category(ch)[0] in "LMN":
            cur.append(ch)
        elif cur:
            parts.append("".join(cur))
            cur = []
    if cur:
        parts.append("".join(cur))
    return "-".join(parts).lower()[:50] or "meeting"


def local_date(start: str) -> str:
    d = parse_iso(start)
    return (d.astimezone() if d else datetime.now()).strftime("%Y-%m-%d")


EXPORT_LINE = re.compile(r"^\[([^\]]+)\] exported: id=(\d+) paths=(.*)$")
_export_cache: dict = {"sig": None, "by_id": {}}


def _export_log(db: Path) -> dict[int, list[tuple[float, Path]]]:
    """Muesli's export log as {meeting id: [(time, .md path), ...]}; parsed again only when it changes."""
    log = db.parent / "meeting-markdown-export.log"
    try:
        st = log.stat()
    except OSError:
        return {}
    sig = (str(log), st.st_size, st.st_mtime)
    if _export_cache["sig"] != sig:
        by_id: dict[int, list[tuple[float, Path]]] = {}
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            m = EXPORT_LINE.match(line.strip())
            d = parse_iso(m.group(1)) if m else None
            if not d:
                continue
            for part in re.split(r", (?=/)", m.group(3)):
                if part.strip().lower().endswith(".md"):
                    by_id.setdefault(int(m.group(2)), []).append((d.timestamp(), Path(part.strip())))
        _export_cache.update(sig=sig, by_id=by_id)
    return _export_cache["by_id"]


def muesli_export(db: Path, meeting_id: int, since: float) -> Path | None:
    """The Markdown file Muesli auto-exported for this meeting after `since` (the latest one)."""
    found = [p for ts, p in _export_log(db).get(meeting_id, []) if ts >= since - 60]
    return found[-1] if found else None


def meeting_row(con: sqlite3.Connection | None, meeting_id: int) -> dict:
    """The meeting as Muesli has it now (the title often changes after the meeting ends)."""
    close = False
    try:
        if con is None:
            con, close = open_db(S.db), True
        r = con.execute("SELECT title, start_time FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        return dict(r) if r else {}
    except sqlite3.Error:
        return {}
    finally:
        if close and con is not None:
            con.close()


def flush_pending(con: sqlite3.Connection | None) -> None:
    """Renames a saved markdown session after Muesli's export, or lets it go after MUESLI_WAIT_S."""
    with PENDING_LOCK:
        for item in list(PENDING):
            try:
                md = muesli_export(S.db, item["id"], item["ended"])
                if md:
                    new = write_markdown(item["snap"], meeting_row(con, item["id"]), md, replaces=item["path"])
                    PENDING.remove(item)
                    if new != item["path"]:
                        item["path"].unlink(missing_ok=True)
                elif now() - item["ended"] >= S.muesli_wait_s:
                    PENDING.remove(item)   # no export: the file keeps the name it got when the meeting ended
            except Exception as ex:
                PENDING.remove(item)       # the file from the meeting's end stays on disk
                error(f"saving the session: {ex}")


def _md_list(items: list[dict], render) -> str:
    return "\n".join(f"- {clock(it['ts'])} {render(it)}" for it in items) or "- (none)"


def write_markdown(snap: dict, row: dict, muesli_md: Path | None, replaces: Path | None = None) -> Path:
    meeting = snap.get("meeting") or {}
    title = row.get("title") or meeting.get("title") or "Meeting"
    start = row.get("start_time") or meeting.get("start") or ""
    stem = muesli_md.stem if muesli_md else f"{local_date(start)}-{muesli_slug(title)}"
    S.sessions_dir.mkdir(parents=True, exist_ok=True)
    path = S.sessions_dir / f"{stem}-copilot.md"
    n = 2
    while path.exists() and path != replaces:
        path = S.sessions_dir / f"{stem}-copilot-{n}.md"
        n += 1
    d = parse_iso(start)
    when = d.astimezone().strftime("%Y-%m-%d %H:%M") if d else start
    m = snap.get("metrics") or {}
    if muesli_md:
        try:
            rel = os.path.relpath(muesli_md, path.parent)
        except ValueError:
            rel = str(muesli_md)
        transcript_line = f"- Transcript (Muesli): [{muesli_md.name}](<{rel}>)"
    else:
        transcript_line = "- Transcript: in Muesli (its export was not found yet)"
    goals = "\n".join(f"- [{'x' if g.get('covered') else ' '}] {g['text']}" for g in snap.get("goals", []))
    out = [
        "---",
        f"title: {json.dumps(title + ' (copilot)', ensure_ascii=False)}",
        "source: meeting-copilot",
        "kind: ai-suggestions",
        "note: AI-generated suggestions shown during the meeting. Not facts; the transcript is the record.",
        f"meeting_id: {meeting.get('id', '')}",
        f"transcript: {json.dumps(str(muesli_md) if muesli_md else '', ensure_ascii=False)}",
        "---",
        "",
        f"# {title} (copilot)",
        "",
        "> Written by AI during the meeting. These are suggestions and guesses, not facts. "
        "Check the transcript before you rely on any of them.",
        "",
        f"- Started: {when}" + (f" · {m['minutes']} min" if m.get("minutes") is not None else ""),
        transcript_line,
    ]
    if m:
        out.append(f"- You spoke {m.get('you_pct', '?')}% · {m.get('questions', '?')} questions · "
                   f"{m.get('wpm', '?')} words/min")
    if snap.get("goal") or goals:
        out += ["", "## Goal", "", snap.get("goal") or "(none)"]
        if goals:
            out += ["", goals]
    out += ["", "## Say this", "", _md_list(snap.get("say", []), lambda it: it["text"]),
            "", "## Ask this", "", _md_list(snap.get("ask", []), lambda it: it["text"]),
            "", "## Corrections", "",
            _md_list(snap.get("corrections", []),
                     lambda it: f"\u201c{it['quote']}\u201d: {it.get('problem', '')} \u2192 {it.get('suggestion', '')}"),
            "", "## Facts found", "", _md_list(snap.get("data", []), lambda it: it["markdown"].replace("\n", " ")),
            "", "## Warnings", "", _md_list(snap.get("warnings", []), lambda it: it["text"]), ""]
    _write_private(path, "\n".join(out))
    print(f"session saved to {path}", flush=True)
    return path


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
                        if E.ai_for == "next":   # a click before the meeting counts for it, if recent
                            E.ai_for = live["id"] if E.ai_armed() else None
                        E.status = "live"
                    print(f"meeting {live['id']} '{live['title']}' started", flush=True)
                elif not live and current:
                    save_session()
                    with E.lock:
                        E.status = f"meeting {current['id']} ended · waiting for the next one"
                        E.meeting = None
                    print(f"meeting {current['id']} ended", flush=True)
                if PENDING:
                    flush_pending(con)
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

            if S.claude and E.ai_allowed() and not q.busy:
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
            elif self.path == "/ai":
                # {"on": true, "meeting": <id shown on the page> | null (= the next meeting)}
                mid = body.get("meeting")
                if body.get("on") is not True:
                    E.ai_for = None
                elif mid is None and E.meeting is None:
                    E.ai_for, E.ai_next_at = "next", now()
                elif isinstance(mid, int) and not isinstance(mid, bool) and E.meeting and mid == E.meeting["id"]:
                    E.ai_for = mid
                else:   # the page showed another meeting than the live one: let it refresh first
                    return self._send(409, b"meeting changed", "text/plain")
            else:
                return self._send(404, b"", "text/plain")
        self._send(200, b'{"ok":true}', "application/json")


def _stop(*_):
    raise KeyboardInterrupt


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse-DNS lookup in server_bind.

    HTTPServer.server_bind calls socket.getfqdn(), which can block for many seconds on some
    Macs (GitHub's macOS runners hang there). We only serve 127.0.0.1, so the name is fixed."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def main(argv=None) -> None:
    global S
    ap = argparse.ArgumentParser(description="Live meeting copilot (reads the Muesli app's live transcript).")
    ap.add_argument("--db", type=Path, help="Muesli database (opened read-only)")
    ap.add_argument("--port", type=int, help="local port (default 4390)")
    ap.add_argument("--version", action="version", version=f"meeting-copilot {__version__}")
    ap.add_argument("--get", metavar="KEY", help=argparse.SUPPRESS)  # for bin/meeting-copilot: PORT, OPEN_CMD
    args = ap.parse_args(argv)
    seed_config()   # bin/meeting-copilot calls --get first, so any first command creates it
    cfg = load_config()
    if args.get:
        v = cfg.get("MEETING_COPILOT_" + args.get, "")
        print(_expand(v) if v and args.get == "DATA_DIR" else v)
        return
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
    srv = _Server(("127.0.0.1", S.port), Page)
    print(f"meeting-copilot on http://127.0.0.1:{S.port}  (database: {S.db})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        save_session(final=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
