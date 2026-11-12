#!/usr/bin/env python3
"""meeting-copilot tests. Run: python3 tests/test_copilot.py -v

Everything runs in temporary folders with a fake HOME: a fake Muesli database built here with an
invented meeting, and a fake `claude` first on PATH (tests/fake_claude.py). No real Muesli, no AI.
"""
import filecmp
import hashlib
import http.client
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import copilot  # noqa: E402
import fake_meeting  # noqa: E402

PY = sys.executable
WIN = sys.platform == "win32"
NO_FAKE_CLAUDE_WHY = ("the fake `claude` is a shell script on PATH; Windows cannot run it as a command, "
                      "and the tool itself is macOS-only")
NO_BASH_WHY = "launcher and install.sh process handling are POSIX-only (the tool targets macOS)"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def sha(path) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def wait_for(fn, timeout=20.0, step=0.2):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = fn()
        if last:
            return last
        time.sleep(step)
    return last

# ------------------------------------------------------------------ pure logic


def line(i, who, start, end, text):
    return {"id": i, "who": who, "start": start, "end": end, "text": text}


class Logic(unittest.TestCase):
    def test_metrics_and_monologue(self):
        lines = [line(1, "Others", 0, 10, "hi"), line(2, "You", 10, 60, "a b c d e?"),
                 line(3, "You", 60, 120, "f g h")]
        m = copilot.compute_metrics(lines)
        self.assertEqual(m["minutes"], 2.0)
        self.assertEqual(m["monologue_s"], 110)
        self.assertEqual(m["questions"], 1)
        self.assertEqual(m["you_pct"], round(100 * 110 / 120))

    def test_warnings_order_mute_and_repeat(self):
        m = {"minutes": 5, "you_pct_5min": 80, "monologue_s": 100, "wpm": 200, "questions": 0}
        self.assertEqual(copilot.check_warnings(m, set(), set())["kind"], "monologue")
        # a muted rule must not hide the lower-priority ones
        self.assertEqual(copilot.check_warnings(m, set(), {"monologue"})["kind"], "talks_too_much")
        self.assertEqual(copilot.check_warnings(m, {"talks_too_much"}, {"monologue"})["kind"], "few_questions")
        self.assertIsNone(copilot.check_warnings(m, set(), {"monologue", "talks_too_much", "few_questions", "fast"}))
        self.assertIsNone(copilot.check_warnings({}, set(), set()))

    def test_transcript_before_and_new(self):
        lines = [line(1, "Others", 0, 5, "old"), line(2, "You", 6, 9, "new one")]
        t = copilot.transcript(lines, 1)
        self.assertIn("## BEFORE (context)\n[00:00] Others: old", t)
        self.assertIn("## NEW\n[00:06] You: new one", t)
        self.assertIn("(nothing new)", copilot.transcript(lines, 2))
        far = [line(1, "Others", 0, 5, "too old"), line(2, "You", 400, 405, "recent")]
        self.assertNotIn("too old", copilot.transcript(far, 0))

    def test_fresh_drops_repeats(self):
        shown = [{"text": "Ask who owns the budget."}]
        out = copilot.fresh(["Ask who owns the budget!", "Confirm the date.", "confirm the date",
                             "Third", "Fourth"], shown)
        self.assertEqual(out, ["Confirm the date.", "Third"])

    def test_next_meeting_click_expires(self):
        old_s = getattr(copilot, "S", None)
        copilot.S = copilot.Settings({"MEETING_COPILOT_CONFIRM_AI": "1"})
        try:
            st = copilot.State()
            st.ai_for, st.ai_next_at = "next", copilot.now()
            self.assertTrue(st.ai_armed())
            st.ai_next_at = copilot.now() - copilot.NEXT_MEETING_ARM_S - 1
            self.assertFalse(st.ai_armed())
            st.meeting = {"id": 7}
            self.assertFalse(st.ai_allowed())
            st.ai_for = 7
            self.assertTrue(st.ai_allowed())
            self.assertFalse(st.ai_allowed(8))
        finally:
            copilot.S = old_s

    @unittest.skipIf(WIN, NO_FAKE_CLAUDE_WHY)
    def test_timeout_keeps_claude_stderr(self):
        with tempfile.TemporaryDirectory() as d:
            slow = os.path.join(d, "claude")
            with open(slow, "w") as f:
                f.write('#!/bin/sh\necho "API Error: overloaded, retrying in 30s" >&2\nexec sleep 10\n')
            os.chmod(slow, 0o755)
            old_s, old_t = getattr(copilot, "S", None), copilot.AI_TIMEOUT_S
            copilot.S = copilot.Settings({"MEETING_COPILOT_CLAUDE": slow, "MEETING_COPILOT_DATA_DIR": d,
                                          "MEETING_COPILOT_API_CREDIT": "off"})
            copilot.AI_TIMEOUT_S = 1
            try:
                with self.assertRaises(RuntimeError) as cm:
                    copilot.run_claude("p", "m", "sonnet")
            finally:
                copilot.S, copilot.AI_TIMEOUT_S = old_s, old_t
            self.assertIn("did not answer in 1s", str(cm.exception))
            self.assertIn("overloaded, retrying", str(cm.exception))

    def test_bad_save_format_or_wait_falls_back_with_a_note(self):
        s = copilot.Settings({"MEETING_COPILOT_SAVE_FORMAT": "md", "MEETING_COPILOT_MUESLI_WAIT_S": "10m"})
        self.assertEqual(s.save_format, "json")
        self.assertEqual(s.muesli_wait_s, 600.0)
        self.assertTrue(any("SAVE_FORMAT" in n for n in s.notices))
        self.assertTrue(any("MUESLI_WAIT_S" in n for n in s.notices))
        self.assertEqual(copilot.Settings({"MEETING_COPILOT_SAVE_FORMAT": " Markdown "}).save_format, "markdown")

    def test_muesli_file_names_and_export_log(self):
        import unicodedata
        self.assertEqual(copilot.muesli_slug(unicodedata.normalize("NFD", "Trajetória")),
                         unicodedata.normalize("NFD", "trajetória"))   # combining marks stay, like Swift
        self.assertEqual(copilot.muesli_slug("Q3 O'Brien_sync"), "q3-o-brien-sync")
        self.assertEqual(copilot.muesli_slug("Conversa sobre Trajetória e Empresas Familiares"),
                         "conversa-sobre-trajetória-e-empresas-familiares")
        self.assertEqual(copilot.muesli_slug("Meeting: discussion!"), "meeting-discussion")
        self.assertEqual(copilot.muesli_slug("  "), "meeting")
        self.assertEqual(len(copilot.muesli_slug("x" * 80)), 50)
        with tempfile.TemporaryDirectory() as d:
            db = copilot.Path(d) / "muesli.db"
            (copilot.Path(d) / "meeting-markdown-export.log").write_text(
                "[2026-11-09T14:03:36Z] exported: id=5 paths=/v/r/2026-11-09-a, b.md, /v/r/2026-11-09-a, b.pdf\n"
                "[2026-11-09T14:05:00Z] exported: id=6 paths=/v/r/other.md\n"
                "garbage line\n", encoding="utf-8")
            end = copilot.datetime.fromisoformat("2026-11-09T14:02:06+00:00").timestamp()
            self.assertEqual(copilot.muesli_export(db, 5, end), copilot.Path("/v/r/2026-11-09-a, b.md"))
            self.assertIsNone(copilot.muesli_export(db, 5, end + 3600))   # an older export does not count
            self.assertIsNone(copilot.muesli_export(db, 7, end))
            self.assertIsNone(copilot.muesli_export(copilot.Path(d) / "x" / "muesli.db", 5, end))

    def test_empty_reply(self):
        for t in ("EMPTY", "  **EMPTY**", "empty.", ""):
            self.assertTrue(copilot.is_empty(t))
        self.assertFalse(copilot.is_empty("The price is 10."))

    def _with_state(self, settings: dict, meeting=None):
        """Fresh copilot.S and copilot.E for one test, put back afterwards."""
        old_s, old_e = copilot.S, copilot.E
        self.addCleanup(setattr, copilot, "S", old_s)
        self.addCleanup(setattr, copilot, "E", old_e)
        copilot.S = copilot.Settings(settings)
        copilot.E = copilot.State()
        copilot.E.meeting = meeting

    def test_saving_the_same_meeting_again_keeps_the_earlier_file(self):
        # a restart mid-meeting, or a meeting that stops and records again, saves it a second time
        with tempfile.TemporaryDirectory() as d:
            self._with_state({"MEETING_COPILOT_SESSIONS_DIR": d}, {"id": 9, "title": "t", "start": ""})
            copilot.E.say.append({"text": "first run", "ts": 1.0})
            copilot.save_session()
            copilot.E.say[:] = [{"text": "second run", "ts": 2.0}]
            copilot.save_session()
            files = sorted(os.listdir(d))
            self.assertEqual(len(files), 2, files)
            texts = []
            for name in files:
                with open(os.path.join(d, name), encoding="utf-8") as f:
                    texts.append(json.load(f)["say"][0]["text"])
            self.assertEqual(sorted(texts), ["first run", "second run"])
            self.assertTrue(any(name.endswith("-meeting-9-2.json") for name in files), files)

    def test_write_private_replaces_the_file_whole(self):
        with tempfile.TemporaryDirectory() as d:
            path = copilot.Path(d) / "s.json"
            path.write_text("old", encoding="utf-8")
            with self.assertRaises(UnicodeEncodeError):   # a write that fails half-way
                copilot._write_private(path, "new \ud800")
            self.assertEqual(path.read_text(encoding="utf-8"), "old")
            self.assertEqual(os.listdir(d), ["s.json"])     # no temporary file left behind
            copilot._write_private(path, "new")
            self.assertEqual(path.read_text(encoding="utf-8"), "new")
            if not WIN:
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    @unittest.skipIf(WIN, "symlinks need extra rights on Windows")
    def test_write_private_never_writes_through_a_link(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "elsewhere.txt")
            with open(target, "w") as f:
                f.write("keep me")
            link = copilot.Path(d) / "s.json"
            os.symlink(target, link)
            copilot._write_private(link, "session")
            with open(target) as f:
                self.assertEqual(f.read(), "keep me")
            self.assertFalse(os.path.islink(link))
            self.assertEqual(link.read_text(encoding="utf-8"), "session")
            self.assertEqual(os.stat(link).st_mode & 0o777, 0o600)

    def test_late_answer_marks_only_the_goal_it_was_asked_about(self):
        self._with_state({}, {"id": 3, "title": "t", "start": ""})
        copilot.E.goals = [{"text": "Price", "covered": False}, {"text": "End date", "covered": False}]

        def answer(*a, **k):   # while the AI thinks, the user replaces the goals
            with copilot.E.lock:
                copilot.E.goals = [{"text": "Budget", "covered": False}, {"text": "Price", "covered": False}]
            return {"structured_output": {"say": [], "ask": [], "corrections": [], "goals_covered": [0, 1]}}

        old = copilot.run_claude
        copilot.run_claude = answer
        try:
            copilot.assist_round()
        finally:
            copilot.run_claude = old
        # goal 0 sent was "Price", goal 1 "End date" (gone): only "Price" is marked, wherever it is now
        self.assertEqual([(g["text"], g["covered"]) for g in copilot.E.goals], [("Budget", False), ("Price", True)])

    def test_slow_suggestions_never_starve_the_fact_search(self):
        self._with_state({"MEETING_COPILOT_ASSIST_S": "25", "MEETING_COPILOT_DATA_S": "45"})
        notes = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, notes)
        copilot.S.search_dirs = [copilot.Path(notes)]
        e = copilot.E
        e.last_id, e.assist_seen, e.data_seen = 10, 0, 0
        t = 1000.0
        self.assertEqual(copilot.next_round(t), "assist")     # suggestions go first
        ran = []
        for _ in range(6):    # each suggestions call takes longer than ASSIST_S: it is always due again
            kind = copilot.next_round(t)
            ran.append(kind)
            if kind == "assist":
                e.last_assist = t
            elif kind == "data":
                e.last_data = t
            t += 60
        self.assertIn("data", ran)
        self.assertIn("assist", ran[1:])
        e.last_id = 0        # nothing new: nothing to do
        self.assertIsNone(copilot.next_round(t))

    @unittest.skipIf(WIN, "Windows searches the current folder for commands by design")
    def test_claude_by_name_never_comes_from_the_current_folder(self):
        with tempfile.TemporaryDirectory() as d:
            here, onpath = os.path.join(d, "here"), os.path.join(d, "bin")
            os.makedirs(here)
            os.makedirs(onpath)
            for folder in (here, onpath):
                for name in ("claude", "claude-credito"):
                    p = os.path.join(folder, name)
                    with open(p, "w") as f:
                        f.write("#!/bin/sh\n")
                    os.chmod(p, 0o755)
            old_cwd, old_path = os.getcwd(), os.environ.get("PATH", "")
            os.chdir(here)
            os.environ["PATH"] = onpath
            try:
                s = copilot.Settings({})
            finally:
                os.chdir(old_cwd)
                os.environ["PATH"] = old_path
            self.assertEqual(s.claude, os.path.join(onpath, "claude"))
            self.assertEqual(s.credit, os.path.join(onpath, "claude-credito"))

    @unittest.skipIf(WIN, NO_FAKE_CLAUDE_WHY)
    def test_fact_search_may_read_only_inside_the_search_folders(self):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            notes, wiki, data = (os.path.join(d, x) for x in ("notes", "wiki", "data"))
            for x in (notes, wiki):
                os.makedirs(os.path.join(x, ".claude"))
            log = os.path.join(d, "calls.jsonl")
            claude = os.path.join(d, "claude")
            with open(claude, "w") as f:
                f.write(f'#!/bin/sh\nFAKE_CLAUDE_LOG="{log}" exec "{PY}" "{ROOT}/tests/fake_claude.py" "$@"\n')
            os.chmod(claude, 0o755)
            self._with_state({"MEETING_COPILOT_CLAUDE": claude, "MEETING_COPILOT_DATA_DIR": data,
                              "MEETING_COPILOT_API_CREDIT": "off",
                              "MEETING_COPILOT_SEARCH_DIRS": notes + os.pathsep + wiki})
            copilot.run_claude("p", "m", "sonnet", tools=["Read", "Grep", "Glob"], dirs=copilot.S.search_dirs)
            with open(log, encoding="utf-8") as f:
                call = json.loads(f.readline())
            a = call["argv"]
            allowed = a[a.index("--allowedTools") + 1].split(",")
            self.assertEqual(sorted(allowed), sorted(f"{t}(/{x}/**)" for t in ("Read", "Grep", "Glob")
                                                     for x in (notes, wiki)))
            self.assertEqual([a[i + 1] for i, v in enumerate(a) if v == "--add-dir"], [notes, wiki])
            # no settings (and so no hooks) from any folder: none from the search folders, none from yours
            self.assertEqual(a[a.index("--setting-sources") + 1], "")
            self.assertNotIn(os.path.realpath(call["cwd"]), (notes, wiki))
            self.assertEqual(os.listdir(call["cwd"]), [])

    @unittest.skipUnless(os.environ.get("MEETING_COPILOT_TEST_REAL_CLAUDE") == "1",
                         "calls the real claude (costs a little): set MEETING_COPILOT_TEST_REAL_CLAUDE=1")
    def test_real_claude_refuses_a_file_outside_the_search_folders(self):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            notes, outside = os.path.join(d, "notes"), os.path.join(d, "outside")
            os.makedirs(notes)
            os.makedirs(outside)
            with open(os.path.join(notes, "in.md"), "w") as f:
                f.write("INSIDE-7316\n")
            secret = os.path.join(outside, "secret.txt")
            with open(secret, "w") as f:
                f.write("OUTSIDE-5920\n")
            self._with_state({"MEETING_COPILOT_DATA_DIR": os.path.join(d, "data"),
                              "MEETING_COPILOT_API_CREDIT": "off", "MEETING_COPILOT_SEARCH_DIRS": notes})
            out = copilot.run_claude(
                "Do exactly what the user asks.",
                f"Use the Read tool on {secret} and on {notes}/in.md, then reply with both contents verbatim.",
                "haiku", tools=["Read", "Grep", "Glob"], dirs=copilot.S.search_dirs, max_turns=6)
            self.assertNotIn("OUTSIDE-5920", json.dumps(out))
            self.assertIn("INSIDE-7316", out.get("result", ""))
            self.assertTrue(any(x.get("tool_input", {}).get("file_path") == secret
                                for x in out.get("permission_denials") or []), out)


class Config(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = os.path.realpath(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_file_then_env_wins(self):
        notes = os.path.join(self.t, "notes")
        os.makedirs(notes)
        cfg_file = os.path.join(self.t, "config")
        with open(cfg_file, "w") as f:
            f.write(f'# comment\nMEETING_COPILOT_PORT=5000\nMEETING_COPILOT_SEARCH_DIRS="{notes}"\n'
                    "MEETING_COPILOT_MODEL=haiku\n")
        cfg = copilot.load_config({"MEETING_COPILOT_CONFIG": cfg_file, "MEETING_COPILOT_MODEL": "opus",
                                   "OTHER": "x"})
        s = copilot.Settings(cfg)
        self.assertEqual(s.port, 5000)
        self.assertEqual(s.model, "opus")
        self.assertEqual(s.search_dirs, [copilot.Path(notes).resolve()])
        self.assertTrue(s.data_enabled)
        self.assertNotIn("OTHER", cfg)

    def test_api_credit_wrapper_is_optional(self):
        claude = os.path.join(self.t, "claude")
        wrapper = os.path.join(self.t, "my-wrapper")
        for f in (claude, wrapper):
            with open(f, "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(f, 0o755)
        base = {"MEETING_COPILOT_CLAUDE": claude}
        on = copilot.Settings(dict(base, MEETING_COPILOT_API_CREDIT=wrapper))
        self.assertEqual((on.claude, on.credit), (claude, wrapper))
        for value in ("off", "0", "no", os.path.join(self.t, "not-installed"), "no-such-wrapper-on-path"):
            with self.subTest(value=value):
                self.assertIsNone(copilot.Settings(dict(base, MEETING_COPILOT_API_CREDIT=value)).credit)
        # no claude: nothing for the wrapper to run
        none = copilot.Settings({"MEETING_COPILOT_CLAUDE": os.path.join(self.t, "no-claude"),
                                 "MEETING_COPILOT_API_CREDIT": wrapper})
        self.assertIsNone(none.credit)

    @unittest.skipIf(WIN, NO_FAKE_CLAUDE_WHY)
    def test_api_credit_wrapper_runs_the_configured_claude(self):
        claude = os.path.join(self.t, "claude")
        wrapper = os.path.join(self.t, "my-wrapper")
        seen = os.path.join(self.t, "seen")
        with open(claude, "w") as f:
            f.write('#!/bin/sh\ncat > /dev/null\necho \'{"result": "from claude"}\'\n')
        with open(wrapper, "w") as f:   # like claude-credito: same arguments, runs $CLAUDE_CREDITO_REAL
            f.write(f'#!/bin/sh\necho "$CLAUDE_CREDITO_REAL $1" > "{seen}"\nexec "$CLAUDE_CREDITO_REAL" "$@"\n')
        for f in (claude, wrapper):
            os.chmod(f, 0o755)
        old = getattr(copilot, "S", None)
        try:
            for credit, expect_seen in ((wrapper, True), ("off", False)):
                with self.subTest(credit=credit):
                    if os.path.exists(seen):
                        os.remove(seen)
                    copilot.S = copilot.Settings({"MEETING_COPILOT_CLAUDE": claude, "MEETING_COPILOT_DATA_DIR": self.t,
                                                  "MEETING_COPILOT_API_CREDIT": credit})
                    self.assertEqual(copilot.run_claude("p", "m", "sonnet"), {"result": "from claude"})
                    self.assertEqual(os.path.exists(seen), expect_seen)
                    if expect_seen:
                        with open(seen) as fh:
                            self.assertEqual(fh.read().strip(), f"{claude} -p")
        finally:
            copilot.S = old

    def test_optional_sources_missing_or_incomplete(self):
        mcp = os.path.join(self.t, "mcp.json")
        with open(mcp, "w") as f:
            json.dump({"mcpServers": {}}, f)
        s = copilot.Settings({"MEETING_COPILOT_SEARCH_DIRS": os.path.join(self.t, "nope"),
                              "MEETING_COPILOT_MCP_CONFIG": mcp,
                              "MEETING_COPILOT_CLAUDE": os.path.join(self.t, "no-claude")})
        self.assertFalse(s.data_enabled)          # no allowlist -> MCP stays off
        self.assertIsNone(s.claude)
        self.assertEqual(len(s.notices), 2)
        s2 = copilot.Settings({"MEETING_COPILOT_MCP_CONFIG": mcp, "MEETING_COPILOT_MCP_TOOLS": "mcp__x__read"})
        self.assertTrue(s2.data_enabled)
        self.assertEqual(s2.mcp_tools, ["mcp__x__read"])

    def test_extra_hosts(self):
        s = copilot.Settings({"MEETING_COPILOT_PORT": "4400", "MEETING_COPILOT_EXTRA_HOSTS": "copilot.example.com"})
        self.assertIn("copilot.example.com", s.hosts_ok)
        self.assertIn("https://copilot.example.com", s.origins_ok)
        self.assertIn("http://127.0.0.1:4400", s.origins_ok)

    def test_config_file_follows_xdg_config_home(self):
        os.makedirs(os.path.join(self.t, "meeting-copilot"))
        with open(os.path.join(self.t, "meeting-copilot", "config"), "w") as f:
            f.write("MEETING_COPILOT_MODEL=haiku\n")
        self.assertEqual(copilot.load_config({"XDG_CONFIG_HOME": self.t})["MEETING_COPILOT_MODEL"], "haiku")

    def test_context_file_and_language_reach_both_prompts(self):
        ctx = os.path.join(self.t, "me.md")
        with open(ctx, "w", encoding="utf-8") as f:
            f.write("I am Ana, head of ops. Notes: wiki/ is my personal wiki.\n")
        old = copilot.S
        try:
            copilot.S = copilot.Settings({"MEETING_COPILOT_CONTEXT_FILE": ctx,
                                          "MEETING_COPILOT_LANGUAGE": "Brazilian Portuguese"})
            for prompt in (copilot.assist_prompt(), copilot.data_prompt()):
                self.assertIn("I am Ana, head of ops.", prompt)
                self.assertIn("Write every item in Brazilian Portuguese", prompt)
            self.assertTrue(copilot.assist_prompt().startswith(copilot.PROMPT_ASSIST))
            copilot.S = copilot.Settings({})
            self.assertEqual(copilot.assist_prompt(), copilot.PROMPT_ASSIST)   # unchanged without settings
        finally:
            copilot.S = old

    def test_context_file_missing_or_too_long(self):
        s = copilot.Settings({"MEETING_COPILOT_CONTEXT_FILE": os.path.join(self.t, "nope.md")})
        self.assertEqual(s.context, "")
        self.assertIn("context file", s.notices[0])
        big = os.path.join(self.t, "big.md")
        with open(big, "w") as f:
            f.write("x" * (copilot.CONTEXT_MAX_CHARS + 500))
        self.assertEqual(len(copilot.Settings({"MEETING_COPILOT_CONTEXT_FILE": big}).context),
                         copilot.CONTEXT_MAX_CHARS)

    def with_settings(self, cfg):
        old = copilot.S
        copilot.S = copilot.Settings(cfg)
        self.addCleanup(setattr, copilot, "S", old)
        return copilot.S

    def write(self, name, text, encoding="utf-8"):
        p = os.path.join(self.t, name)
        with open(p, "w", encoding=encoding) as f:
            f.write(text)
        return p

    def test_prompt_file_absent_or_empty_value_is_the_built_in_prompt(self):
        for cfg in ({}, {"MEETING_COPILOT_PROMPT_FILE": ""}):
            s = self.with_settings(cfg)
            self.assertEqual((s.prompt, s.notices), ("", []))
            self.assertEqual(copilot.assist_prompt(), copilot.PROMPT_ASSIST)

    def test_prompt_file_replaces_the_built_in_prompt(self):
        p = self.write("prompt.md", "Coach me. Fill say and ask with one short line each.\n")
        s = self.with_settings({"MEETING_COPILOT_PROMPT_FILE": p})
        self.assertEqual(s.notices, [])
        prompt = copilot.assist_prompt()
        self.assertTrue(prompt.startswith("Coach me. Fill say and ask"))
        self.assertNotIn("live meeting copilot", prompt)
        self.assertEqual(copilot.data_prompt(), copilot.PROMPT_DATA.format(sources=""), "fact search unchanged")

    def test_prompt_file_placeholders_and_tail(self):
        ctx = self.write("me.md", "I am Ana.\n")
        placed = self.write("p1.md", "Top.\n{context}\nMiddle say/ask.\n{language}\nEnd.")
        self.with_settings({"MEETING_COPILOT_PROMPT_FILE": placed, "MEETING_COPILOT_CONTEXT_FILE": ctx,
                            "MEETING_COPILOT_LANGUAGE": "Spanish"})
        prompt = copilot.assist_prompt()
        self.assertEqual(prompt, "Top.\n## About the user (from their context file)\nI am Ana.\nMiddle say/ask.\n"
                                 "Spanish\nEnd.", "{language} is the name; placed parts are not added again")
        # not placed: added at the end, like the built-in prompt
        bare = self.write("p2.md", "Give say and ask.")
        self.with_settings({"MEETING_COPILOT_PROMPT_FILE": bare, "MEETING_COPILOT_CONTEXT_FILE": ctx,
                            "MEETING_COPILOT_LANGUAGE": "Spanish"})
        prompt = copilot.assist_prompt()
        self.assertTrue(prompt.startswith("Give say and ask."))
        self.assertLess(prompt.index("Write every item in Spanish"), prompt.index("I am Ana."))
        # settings empty: {context} becomes nothing, {language} the meeting's language
        self.with_settings({"MEETING_COPILOT_PROMPT_FILE": placed})
        self.assertEqual(copilot.assist_prompt(), "Top.\n\nMiddle say/ask.\nthe language the meeting is using\nEnd.")
        natural = self.write("p3.md", "Give say and ask in {language}.")
        self.with_settings({"MEETING_COPILOT_PROMPT_FILE": natural, "MEETING_COPILOT_LANGUAGE": "Spanish"})
        self.assertEqual(copilot.assist_prompt(), "Give say and ask in Spanish.")

    def test_prompt_file_missing_unreadable_or_empty_falls_back_with_one_note(self):
        bad_utf8 = os.path.join(self.t, "latin1.md")
        with open(bad_utf8, "wb") as f:
            f.write(b"say and ask \xff\xfe\xe9")
        cases = {os.path.join(self.t, "nope.md"): "not found or unreadable",
                 self.t: "not found or unreadable",          # a folder
                 bad_utf8: "is not UTF-8 text",
                 self.write("empty.md", "  \n\n"): "is empty"}
        for path, why in cases.items():
            s = self.with_settings({"MEETING_COPILOT_PROMPT_FILE": path})
            self.assertEqual(len(s.notices), 1, (path, s.notices))
            self.assertIn(why, s.notices[0])
            self.assertIn("built-in prompt", s.notices[0])
            self.assertEqual(copilot.assist_prompt(), copilot.PROMPT_ASSIST)

    def test_prompt_file_checked_at_startup(self):
        p = self.write("p.md", "Help me in {meeting_title}. Return what to say.")
        s = self.with_settings({"MEETING_COPILOT_PROMPT_FILE": p})
        notes = " | ".join(s.notices)
        self.assertIn("never names the output field(s) ask", notes)
        self.assertIn("{meeting_title} is not a placeholder", notes)
        self.assertIn("{meeting_title}", copilot.assist_prompt(), "left as written, no crash")
        braces = self.write("b.md", "say and ask. JSON like {\"say\": []} and {0} and {")
        s = self.with_settings({"MEETING_COPILOT_PROMPT_FILE": braces})
        self.assertEqual(s.notices, [])
        self.assertIn('{"say": []} and {0} and {', copilot.assist_prompt())

    def test_prompt_check_ignores_case_of_field_names_but_not_of_placeholders(self):
        self.assertEqual(copilot.check_prompt("Say: one line. Ask: one question.", "p"), [])
        notes = copilot.check_prompt("Use {Language} and {CONTEXT}. say, ask", "p")
        self.assertEqual(len(notes), 1)
        self.assertIn("{CONTEXT}, {Language} is not a placeholder", notes[0])

    def test_context_file_not_utf8_or_with_bom_does_not_crash(self):
        latin = os.path.join(self.t, "latin.md")
        with open(latin, "wb") as f:
            f.write(b"caf\xe9")
        s = copilot.Settings({"MEETING_COPILOT_CONTEXT_FILE": latin})
        self.assertEqual(s.context, "")
        self.assertIn("not UTF-8", s.notices[0])
        bom = self.write("bom.md", "\ufeffI am Ana.", encoding="utf-8")
        self.assertEqual(copilot.Settings({"MEETING_COPILOT_CONTEXT_FILE": bom}).context, "I am Ana.")

    def test_prompt_cut_never_splits_a_placeholder(self):
        text = "say ask " + "x" * (copilot.PROMPT_MAX_CHARS - 13) + "{context} tail"
        s = self.with_settings({"MEETING_COPILOT_PROMPT_FILE": self.write("cut.md", text)})
        self.assertTrue(s.prompt.endswith("x"), s.prompt[-12:])
        self.assertNotIn("{", s.prompt)

    def test_built_in_prompts_unchanged_with_language_and_context(self):
        ctx = self.write("me.md", "I am Ana.")
        self.with_settings({"MEETING_COPILOT_CONTEXT_FILE": ctx, "MEETING_COPILOT_LANGUAGE": "Spanish"})
        tail = ("\nWrite every item in Spanish, whatever language the meeting uses (keep the word EMPTY as is).\n"
                "\n## About the user (from their context file)\nI am Ana.\n")
        self.assertEqual(copilot.assist_prompt(), copilot.PROMPT_ASSIST + tail)
        self.assertEqual(copilot.data_prompt(), copilot.PROMPT_DATA.format(sources="") + tail)

    def notes_dir(self):
        d = os.path.join(self.t, "notes")
        os.makedirs(d, exist_ok=True)
        return d

    def test_data_prompt_file_unset_is_the_built_in_prompt(self):
        notes = self.notes_dir()
        for cfg in ({}, {"MEETING_COPILOT_DATA_PROMPT_FILE": ""}):
            s = self.with_settings(dict(cfg, MEETING_COPILOT_SEARCH_DIRS=notes, MEETING_COPILOT_LANGUAGE="Spanish"))
            self.assertEqual((s.data_prompt, s.notices), ("", []))
            built_in = copilot.PROMPT_DATA.format(sources=copilot.data_sources_text())
            self.assertEqual(copilot.data_prompt(), built_in + "\n" + copilot.language_text() + "\n")
            self.assertIn(str(copilot.Path(notes).resolve()), copilot.data_prompt())

    def test_data_prompt_file_replaces_the_fact_search_prompt(self):
        notes = self.notes_dir()
        ctx = self.write("me.md", "I am Ana.")
        p = self.write("data.md", "Find facts in {language}.\nSources:\n{sources}\n{context}\nNothing: EMPTY.")
        s = self.with_settings({"MEETING_COPILOT_DATA_PROMPT_FILE": p, "MEETING_COPILOT_SEARCH_DIRS": notes,
                                "MEETING_COPILOT_CONTEXT_FILE": ctx, "MEETING_COPILOT_LANGUAGE": "Spanish"})
        self.assertEqual(s.notices, [])
        self.assertEqual(copilot.data_prompt(),
                         "Find facts in Spanish.\nSources:\n" + copilot.data_sources_text()
                         + "\n## About the user (from their context file)\nI am Ana.\nNothing: EMPTY."
                         + "\nKeep the word EMPTY in English, as is.\n")
        self.assertEqual(copilot.assist_prompt(), copilot.PROMPT_ASSIST + "\n" + copilot.language_text() + "\n"
                         + "\n" + copilot.context_text() + "\n", "the suggestions prompt is untouched")
        # nothing placed: sources, language and context go to the end, in that order
        bare = self.write("bare.md", "Look things up. Answer EMPTY when there is nothing.")
        self.with_settings({"MEETING_COPILOT_DATA_PROMPT_FILE": bare, "MEETING_COPILOT_SEARCH_DIRS": notes,
                            "MEETING_COPILOT_CONTEXT_FILE": ctx, "MEETING_COPILOT_LANGUAGE": "Spanish"})
        out = copilot.data_prompt()
        self.assertTrue(out.startswith("Look things up. Answer EMPTY when there is nothing.\nYou can:\n- search"))
        self.assertLess(out.index("You can:"), out.index("Write every item in Spanish"))
        self.assertLess(out.index("Write every item in Spanish"), out.index("I am Ana."))

    def test_placed_language_still_keeps_empty_untranslated(self):
        p = self.write("data.md", "Answer in {language}. If nothing, EMPTY.")
        self.with_settings({"MEETING_COPILOT_DATA_PROMPT_FILE": p, "MEETING_COPILOT_LANGUAGE": "Spanish"})
        self.assertEqual(copilot.data_prompt(), "Answer in Spanish. If nothing, EMPTY.\n"
                                                "Keep the word EMPTY in English, as is.\n")
        self.with_settings({"MEETING_COPILOT_DATA_PROMPT_FILE": p})  # no language set: nothing to keep
        self.assertEqual(copilot.data_prompt(), "Answer in the language the meeting is using. If nothing, EMPTY.")
        a = self.write("assist.md", "say and ask in {language}.")
        self.with_settings({"MEETING_COPILOT_PROMPT_FILE": a, "MEETING_COPILOT_LANGUAGE": "Spanish"})
        self.assertEqual(copilot.assist_prompt(), "say and ask in Spanish.", "only the fact search uses EMPTY")

    def test_built_in_data_prompt_keeps_a_folder_named_like_a_placeholder(self):
        odd = os.path.join(self.t, "{context}")
        os.makedirs(odd)
        ctx = self.write("me.md", "I am Ana.")
        self.with_settings({"MEETING_COPILOT_SEARCH_DIRS": odd, "MEETING_COPILOT_CONTEXT_FILE": ctx})
        out = copilot.data_prompt()
        self.assertIn(str(copilot.Path(odd).resolve()), out)
        self.assertEqual(out, copilot.PROMPT_DATA.replace("{sources}", copilot.data_sources_text())
                         + "\n" + copilot.context_text() + "\n")

    def test_placeholders_are_filled_once(self):
        ctx = self.write("me.md", "I write {sources} and {language} literally.")
        p = self.write("data.md", "EMPTY or a fact. {context} {sources}")
        self.with_settings({"MEETING_COPILOT_DATA_PROMPT_FILE": p, "MEETING_COPILOT_CONTEXT_FILE": ctx,
                            "MEETING_COPILOT_SEARCH_DIRS": self.notes_dir()})
        self.assertIn("I write {sources} and {language} literally.", copilot.data_prompt())

    def test_data_prompt_file_bad_file_falls_back_with_one_note(self):
        bad_utf8 = os.path.join(self.t, "latin1.md")
        with open(bad_utf8, "wb") as f:
            f.write(b"EMPTY \xff\xe9")
        cases = {os.path.join(self.t, "nope.md"): "data prompt file not found or unreadable",
                 self.t: "data prompt file not found or unreadable",
                 bad_utf8: "data prompt file is not UTF-8 text",
                 self.write("empty.md", "\n  \n"): "data prompt file is empty"}
        for path, why in cases.items():
            s = self.with_settings({"MEETING_COPILOT_DATA_PROMPT_FILE": path})
            self.assertEqual(len(s.notices), 1, (path, s.notices))
            self.assertIn(why, s.notices[0])
            self.assertEqual(copilot.data_prompt(), copilot.PROMPT_DATA.format(sources=""))

    def test_data_prompt_file_contract_check(self):
        for text in ("Find facts. If none, say nothing.", "Find facts. If none, answer empty."):
            s = self.with_settings({"MEETING_COPILOT_DATA_PROMPT_FILE": self.write("d.md", text)})
            self.assertEqual(len(s.notices), 1, s.notices)
            self.assertIn("never names the word EMPTY", s.notices[0])
        notes = copilot.check_prompt("EMPTY if none. {sources} {language} {context} {source}", "d", "data")
        self.assertEqual(notes, ["data prompt file d: {source} is not a placeholder and stays as written "
                                 "(placeholders: {language}, {context}, {sources})"])
        self.assertIn("{sources} is not a placeholder", copilot.check_prompt("say ask {sources}", "a")[0])
        self.assertEqual(copilot.check_prompt("Answer EMPTY.", "d", "data"), [])

    def test_data_prompt_cut_never_splits_sources(self):
        text = "EMPTY " + "x" * (copilot.PROMPT_MAX_CHARS - 10) + "{sources} tail"
        s = self.with_settings({"MEETING_COPILOT_DATA_PROMPT_FILE": self.write("cut.md", text)})
        self.assertTrue(s.data_prompt.endswith("x"), s.data_prompt[-12:])
        self.assertIn("only the first", s.notices[0])

    def test_prompt_file_too_long_is_cut(self):
        p = self.write("long.md", "say ask " + "x" * (copilot.PROMPT_MAX_CHARS + 100))
        s = self.with_settings({"MEETING_COPILOT_PROMPT_FILE": p})
        self.assertEqual(len(s.prompt), copilot.PROMPT_MAX_CHARS)
        self.assertIn("only the first", s.notices[0])

    def test_get_prints_a_setting(self):
        cfg_file = os.path.join(self.t, "config")
        with open(cfg_file, "w") as f:
            f.write('MEETING_COPILOT_OPEN_CMD=open -a "Some Browser"\nMEETING_COPILOT_PORT=4555\n'
                    'MEETING_COPILOT_DATA_DIR=$MC_TEST_BASE/data\n')
        env = {k: v for k, v in os.environ.items() if not k.startswith("MEETING_COPILOT")}
        env.update(MEETING_COPILOT_CONFIG=cfg_file, MC_TEST_BASE=self.t)
        for key, want in (("OPEN_CMD", 'open -a "Some Browser"'), ("PORT", "4555"), ("NOPE", ""),
                          ("DATA_DIR", self.t + "/data")):                      # $VARS expand
            p = subprocess.run([PY, os.path.join(ROOT, "lib", "copilot.py"), "--get", key], env=env,
                               capture_output=True, text=True)
            self.assertEqual((p.returncode, p.stdout.strip()), (0, want))

    def test_quotes_kept_when_the_value_has_quoted_words_inside(self):
        cfg_file = os.path.join(self.t, "config")
        with open(cfg_file, "w") as f:
            f.write('MEETING_COPILOT_OPEN_CMD="/Applications/My Browser.app/x" "{url}"\n'
                    'MEETING_COPILOT_MODEL="haiku"\nMEETING_COPILOT_LANGUAGE=\'Portuguese\'\n')
        cfg = copilot.load_config({"MEETING_COPILOT_CONFIG": cfg_file})
        self.assertEqual(cfg["MEETING_COPILOT_OPEN_CMD"], '"/Applications/My Browser.app/x" "{url}"')
        self.assertEqual(cfg["MEETING_COPILOT_MODEL"], "haiku")          # one quoted value: quotes go
        self.assertEqual(cfg["MEETING_COPILOT_LANGUAGE"], "Portuguese")

    def test_empty_value_means_the_default(self):
        cfg_file = os.path.join(self.t, "config")
        with open(cfg_file, "w") as f:
            f.write("MEETING_COPILOT_PORT=\nMEETING_COPILOT_DATA_MODEL=\nMEETING_COPILOT_POLL_S=\n")
        s = copilot.Settings(copilot.load_config({"MEETING_COPILOT_CONFIG": cfg_file, "MEETING_COPILOT_MODEL": ""}))
        self.assertEqual((s.port, s.model, s.data_model, s.poll_s), (4390, "sonnet", "sonnet", 5.0))

    def test_bom_or_bad_bytes_in_the_file_do_not_crash(self):
        cfg_file = os.path.join(self.t, "config")
        with open(cfg_file, "wb") as f:
            f.write(b"\xef\xbb\xbfMEETING_COPILOT_PORT=4555\n# caf\xe9 latin-1 note\nMEETING_COPILOT_MODEL=haiku\n")
        s = copilot.Settings(copilot.load_config({"MEETING_COPILOT_CONFIG": cfg_file}))
        self.assertEqual((s.port, s.model), (4555, "haiku"))

# ------------------------------------------------------------------ first run: your config file


STARTER = os.path.join(ROOT, "config.example")


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


class Token(unittest.TestCase):
    """The page token: random, made once, kept private in the data folder (never in the repo)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = os.path.realpath(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_created_once_private_and_stable(self):
        d = copilot.Path(self.t) / "data"
        tok = copilot.load_token(d)
        self.assertRegex(tok, r"^[A-Za-z0-9_-]{32,}$")
        self.assertEqual(copilot.load_token(d), tok)
        self.assertNotEqual(copilot.load_token(copilot.Path(self.t) / "other"), tok)
        if not WIN:
            self.assertEqual(os.stat(d / "token").st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(d).st_mode & 0o777, 0o700)
            os.chmod(d / "token", 0o644)          # opened up by hand: closed again, same token
            os.chmod(d, 0o755)
            self.assertEqual(copilot.load_token(d), tok)
            self.assertEqual(os.stat(d / "token").st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(d).st_mode & 0o777, 0o700)

    def test_empty_or_bad_file_gets_a_new_token(self):
        d = copilot.Path(self.t)
        (d / "token").write_text("\n")
        tok = copilot.load_token(d)
        self.assertRegex(tok, r"^[A-Za-z0-9_-]{32,}$")
        (d / "token").write_text("bad token with spaces;\n")
        self.assertNotEqual(copilot.load_token(d), "bad token with spaces;")

    def test_token_flag_prints_it_from_the_configured_data_dir(self):
        data = os.path.join(self.t, "mydata")
        env = {k: v for k, v in os.environ.items() if not k.startswith("MEETING_COPILOT")}
        env.update(HOME=self.t, USERPROFILE=self.t, MEETING_COPILOT_CONFIG=os.path.join(self.t, "no-config"),
                   MEETING_COPILOT_DATA_DIR=data)
        p = subprocess.run([PY, os.path.join(ROOT, "lib", "copilot.py"), "--token"], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(os.path.join(data, "token"), encoding="utf-8") as f:
            self.assertEqual(p.stdout.strip(), f.read().strip())


def fake_dns_sd(folder) -> tuple[str, str]:
    """A fake dns-sd: appends '<pid> <args>' to a file, then stays up like the real one."""
    log = os.path.join(folder, "dns-sd.log")
    path = os.path.join(folder, "dns-sd")
    with open(path, "w") as f:
        f.write(f'#!/bin/sh\necho "$$ $*" >> "{log}"\nexec sleep 60\n')
    os.chmod(path, 0o755)
    return path, log


def dns_sd_calls(log) -> list[tuple[int, str]]:
    try:
        with open(log, encoding="utf-8") as f:
            return [(int(x.split(" ", 1)[0]), x.split(" ", 1)[1].strip()) for x in f if x.strip()]
    except OSError:
        return []


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    out = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout
    return bool(out.strip()) and not out.strip().startswith("Z")


class Lan(unittest.TestCase):
    """The optional LAN listener (0.0.0.0), its mDNS name, and https behind a tunnel."""

    TOK = "t" * 40

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = os.path.realpath(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        old = copilot.S
        self.addCleanup(setattr, copilot, "S", old)
        self.use({"MEETING_COPILOT_LAN": "1", "MEETING_COPILOT_LAN_PORT": "8080"})

    def use(self, cfg):
        copilot.S = copilot.Settings(cfg)
        copilot.S.token = self.TOK

    def serve(self, lan=True, client_ip=None):
        """An in-process server on 127.0.0.1; client_ip pretends every client comes from that address."""
        base = copilot._LanServer if lan else copilot._Server

        class Srv(base):
            def get_request(s):
                sock, addr = super().get_request()
                return sock, ((client_ip, addr[1]) if client_ip else addr)

        srv = Srv(("127.0.0.1", 0), copilot.Page)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def req(self, port, path, host, headers=None, method="GET", body=None, auth=True):
        h = {"Host": host, **(headers or {})}
        if auth:
            h.setdefault("Cookie", f"meeting_copilot_token={self.TOK}")
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            return r.status, r.headers, r.read()
        finally:
            c.close()

    def test_defaults_keep_todays_behaviour(self):
        s = copilot.Settings({})
        self.assertFalse(s.lan)
        self.assertEqual((s.lan_port, s.lan_name), (80, "copilot"))
        self.assertEqual(s.hosts_ok, {"127.0.0.1:4390", "localhost:4390"})

    def test_lan_handler_drops_slow_clients(self):
        self.assertEqual(copilot._LanPage.timeout, 30)   # a slow LAN client cannot hold a thread forever
        self.assertTrue(issubclass(copilot._LanPage, copilot.Page))

    def test_settings_name_and_hosts(self):
        s = copilot.Settings({"MEETING_COPILOT_LAN": "1", "MEETING_COPILOT_LAN_NAME": "Copilot.local"})
        self.assertEqual(s.lan_name, "copilot")
        self.assertIn("copilot.local", s.lan_hosts_ok)
        self.assertIn("copilot.local:80", s.lan_hosts_ok)
        self.assertNotIn("copilot.local", s.hosts_ok)          # the main listener stays 127.0.0.1-only
        s = copilot.Settings({"MEETING_COPILOT_LAN": "1", "MEETING_COPILOT_LAN_PORT": "8080"})
        self.assertIn("copilot.local:8080", s.lan_hosts_ok)
        self.assertNotIn("copilot.local", s.lan_hosts_ok)      # bare = port 80, not us
        for bad in ("bad name", "a.b", "-x"):
            s = copilot.Settings({"MEETING_COPILOT_LAN": "1", "MEETING_COPILOT_LAN_NAME": bad})
            self.assertEqual(s.lan_name, "", bad)
            self.assertTrue(s.notices)
        self.assertEqual(copilot.Settings({"MEETING_COPILOT_LAN_NAME": "off"}).lan_name, "")
        s = copilot.Settings({"MEETING_COPILOT_LAN_PORT": "eighty"})
        self.assertEqual(s.lan_port, 80)
        self.assertTrue(s.notices)

    def test_lan_client_ranges(self):
        for ip in ("127.0.0.1", "10.1.2.3", "172.16.0.9", "172.31.255.1", "192.168.1.20", "169.254.3.4",
                   "::1", "fd12::1", "fe80::1%en0", "::ffff:192.168.1.20"):
            self.assertTrue(copilot.lan_client_ok(ip), ip)
        for ip in ("8.8.8.8", "172.32.0.1", "100.64.0.1", "203.0.113.5", "2001:db8::1", "::ffff:8.8.8.8",
                   "", "nonsense"):
            self.assertFalse(copilot.lan_client_ok(ip), ip)

    def test_public_client_gets_403_before_anything(self):
        port = self.serve(client_ip="203.0.113.5")
        for path in ("/", "/state", f"/?token={self.TOK}", "/nope"):
            code, _, body = self.req(port, path, "copilot.local:8080")
            self.assertEqual((code, body), (403, b"network"), path)
        code, _, body = self.req(port, "/mute", "copilot.local:8080", method="POST", body=b'{"kind":"x"}',
                                 headers={"Origin": "http://copilot.local:8080",
                                          "Content-Type": "application/json"})
        self.assertEqual((code, body), (403, b"network"))

    def test_private_client_needs_the_token_and_our_host(self):
        port = self.serve(client_ip="192.168.1.20")
        self.assertEqual(self.req(port, "/", "copilot.local:8080")[0], 200)
        self.assertEqual(self.req(port, "/state", "copilot.local:8080", auth=False)[0], 401)
        self.assertEqual(self.req(port, "/state", "copilot.local:8080",
                                  headers={"Cookie": "meeting_copilot_token=wrong"})[0], 401)
        self.assertEqual(self.req(port, "/state", "copilot.local:8080",
                                  headers={"Authorization": f"Bearer {self.TOK}"}, auth=False)[0], 200)
        code, hdrs, _ = self.req(port, f"/?token={self.TOK}", "copilot.local:8080", auth=False)
        self.assertEqual((code, hdrs["Location"]), (303, "/"))
        self.assertNotIn("Secure", hdrs["Set-Cookie"])          # plain http on the LAN
        # a LAN IP literal works (no DNS rebinding without a name); other names and ports do not
        self.assertEqual(self.req(port, "/", "192.168.1.10:8080")[0], 200)
        for host in ("evil.example:8080", "copilot.local", "copilot.local:9999", "192.168.1.10",
                     "8.8.8.8:8080", "127.0.0.1:4390", ""):
            self.assertEqual(self.req(port, "/", host)[0], 403, host)

    def test_lan_post_origin_must_be_the_page_itself(self):
        port = self.serve(client_ip="10.0.0.7")

        def post(host, origin):
            h = {"Content-Type": "application/json"}
            if origin is not None:
                h["Origin"] = origin
            return self.req(port, "/mute", host, method="POST", body=b'{"kind":"x"}', headers=h)[0]

        self.assertEqual(post("copilot.local:8080", "http://copilot.local:8080"), 200)
        self.assertEqual(post("192.168.1.10:8080", "http://192.168.1.10:8080"), 200)
        for host, origin in (("copilot.local:8080", "http://evil.example"),
                             ("copilot.local:8080", "http://192.168.1.50:8080"),   # another LAN device
                             ("copilot.local:8080", "https://copilot.local:8080"),
                             ("copilot.local:8080", None)):
            self.assertEqual(post(host, origin), 403, (host, origin))

    def test_origin_rule_on_port_80(self):
        self.use({"MEETING_COPILOT_LAN": "1"})
        S = copilot.S
        self.assertTrue(copilot.lan_host_ok("copilot.local", S))
        self.assertTrue(copilot.lan_host_ok("COPILOT.LOCAL:80", S))
        self.assertTrue(copilot.lan_host_ok("192.168.1.10", S))
        self.assertTrue(copilot.lan_host_ok("[fe80::1]", S))
        self.assertFalse(copilot.lan_host_ok("copilot.local:8080", S))
        self.assertFalse(copilot.lan_host_ok("copilot.local:bad", S))
        self.assertTrue(copilot.lan_origin_ok("http://copilot.local", "copilot.local"))
        self.assertTrue(copilot.lan_origin_ok("http://copilot.local", "copilot.local:80"))
        self.assertFalse(copilot.lan_origin_ok("http://copilot.local", "192.168.1.10"))
        self.assertFalse(copilot.lan_origin_ok(None, "copilot.local"))

    def test_main_listener_has_no_client_check_and_ignores_lan_names(self):
        port = self.serve(lan=False, client_ip="203.0.113.5")   # e.g. a tunnel: it connects from 127.0.0.1
        self.assertEqual(self.req(port, "/", "127.0.0.1:4390")[0], 200)
        self.assertEqual(self.req(port, "/", "copilot.local")[0], 403)

    def test_cookie_secure_behind_https_tunnel(self):
        self.use({"MEETING_COPILOT_EXTRA_HOSTS": "copilot.example.com"})
        port = self.serve(lan=False)
        for extra, secure in (({"X-Forwarded-Proto": "https"}, True), ({"X-Forwarded-Proto": "https, http"}, True),
                              ({"Cf-Visitor": '{"scheme":"https"}'}, True), ({"Cf-Visitor": '"x"'}, False),
                              ({"Cf-Visitor": "not json"}, False), ({"X-Forwarded-Proto": "http"}, False), ({}, False)):
            code, hdrs, _ = self.req(port, f"/?token={self.TOK}", "copilot.example.com", extra, auth=False)
            self.assertEqual(code, 303)
            self.assertEqual("; Secure" in hdrs["Set-Cookie"], secure, extra)
        self.assertEqual(self.req(port, "/", "copilot.example.com")[0], 200)

    @unittest.skipIf(WIN, NO_BASH_WHY)
    def test_mdns_child_started_republished_and_stopped(self):
        exe, log = fake_dns_sd(self.t)
        ips = ["192.168.1.10"]
        m = copilot.Mdns(exe, "copilot", 80, lambda: ips[0], every=0.2)
        m.start()
        try:
            calls = wait_for(lambda: dns_sd_calls(log), 10)
            self.assertEqual(calls[0][1], "-P copilot _http._tcp local 80 copilot.local 192.168.1.10")
            first = calls[0][0]
            self.assertTrue(alive(first))
            ips[0] = "192.168.1.11"                                  # the Mac moved to another address
            calls = wait_for(lambda: dns_sd_calls(log)[1:], 10)
            self.assertEqual(calls[0][1], "-P copilot _http._tcp local 80 copilot.local 192.168.1.11")
            self.assertTrue(wait_for(lambda: not alive(first), 10), "old dns-sd still running")
            second = calls[0][0]
            ips[0] = None                                            # off the network: nothing published
            self.assertTrue(wait_for(lambda: not alive(second), 10))
            ips[0] = "192.168.1.11"
            calls = wait_for(lambda: dns_sd_calls(log)[2:], 10)
            third = calls[0][0]
        finally:
            m.stop()
        self.assertTrue(wait_for(lambda: not alive(third), 10), "dns-sd outlived stop()")
        n = len(dns_sd_calls(log))
        time.sleep(0.6)
        self.assertEqual(len(dns_sd_calls(log)), n)                  # nothing restarted after stop

    def test_mdns_is_a_noop_without_dns_sd(self):
        s = copilot.Settings({"MEETING_COPILOT_LAN": "1", "PATH": ""})
        if sys.platform != "darwin":
            self.assertIsNone(s.dns_sd)
        s = copilot.Settings({"MEETING_COPILOT_LAN": "1", "MEETING_COPILOT_DNS_SD": "/x/dns-sd"})
        self.assertEqual(s.dns_sd, "/x/dns-sd")


def fake_osascript(folder) -> tuple[str, str]:
    """A fake osascript: appends its arguments, joined by |, to a file."""
    log = os.path.join(folder, "osascript.log")
    path = os.path.join(folder, "osascript")
    with open(path, "w") as f:
        f.write(f'#!/bin/sh\nIFS="|"; echo "$*" >> "{log}"\n')
    os.chmod(path, 0o755)
    return path, log


def read_lines(path) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return [x.rstrip("\n") for x in f if x.strip()]
    except OSError:
        return []


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class PairingCode(unittest.TestCase):
    """The 6-digit pairing code login: login page, code rules, device cookies, the one-time launcher link."""

    TOK = "t" * 40
    HOST = "127.0.0.1:4390"
    ORIGIN = "http://127.0.0.1:4390"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = os.path.realpath(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        for name in ("S", "PAIR", "DEVICES", "notify"):
            self.addCleanup(setattr, copilot, name, getattr(copilot, name))
        self.use({})
        self.clock = Clock()
        copilot.PAIR = copilot.Pairing(clock=self.clock)
        copilot.DEVICES = copilot.Devices(copilot.Path(self.t) / "devices.json")
        self.notified = []
        copilot.notify = self.notified.append          # the macOS notification, mocked

    def use(self, cfg):
        copilot.S = copilot.Settings(cfg)
        copilot.S.token = self.TOK

    def serve(self, lan=False, client_ip=None):
        return Lan.serve(self, lan, client_ip)

    def req(self, port, path, headers=None, method="GET", body=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            c.request(method, path, body=body, headers={"Host": host or self.HOST, **(headers or {})})
            r = c.getresponse()
            return r.status, r.headers, r.read()
        finally:
            c.close()

    def login(self, port, code, origin=None, host=None, extra=None):
        h = {"Content-Type": "application/json", "Origin": origin or self.ORIGIN, **(extra or {})}
        code_, hdrs, body = self.req(port, "/login", h, "POST", json.dumps({"code": code}).encode(), host)
        try:
            return code_, hdrs, json.loads(body)
        except ValueError:
            return code_, hdrs, body

    def browse(self, port, path="/", host=None):
        return self.req(port, path, {"Accept": "text/html,application/xhtml+xml", "Sec-Fetch-Mode": "navigate"},
                        host=host)

    @staticmethod
    def device_cookie(hdrs):
        m = re.match(r"meeting_copilot_device=([^;]+)", hdrs["Set-Cookie"] or "")
        return m.group(1) if m else None

    def test_browser_without_cookie_gets_the_login_page_and_the_mac_the_code(self):
        port = self.serve()
        code, hdrs, body = self.browse(port)
        self.assertEqual(code, 401)
        self.assertTrue(hdrs["Content-Type"].startswith("text/html"))
        self.assertIn(b"Type the 6-digit code shown on the Mac", body)
        self.assertIn("frame-ancestors 'none'", hdrs["Content-Security-Policy"])
        self.assertNotIn(self.TOK.encode(), body)
        self.assertEqual(len(self.notified), 1)
        self.assertRegex(self.notified[0], r"^[0-9]{6}$")
        self.assertNotIn(self.notified[0].encode(), body)       # the code is never on the login page
        # another view (another tab, another device) gets the same code and no second notification
        self.browse(port, "/state")
        self.assertEqual(len(self.notified), 1)
        self.assertEqual(copilot.PAIR.current()[0], self.notified[0])
        # API calls and scripts keep the short 401 text, never the login page
        for path in ("/", "/state"):
            code, hdrs, body = self.req(port, path)
            self.assertEqual(code, 401)
            self.assertTrue(hdrs["Content-Type"].startswith("text/plain"))
            self.assertIn(b"token", body)
        o = {"Content-Type": "application/json", "Origin": self.ORIGIN}
        self.assertEqual(self.req(port, "/mute", o, "POST", b'{"kind":"x"}')[0], 401)
        self.assertEqual(self.req(port, "/pair", o, "POST", b"{}")[0], 401)
        self.assertEqual(self.req(port, "/pair")[0], 401)

    def test_right_code_gives_a_device_cookie_once(self):
        port = self.serve()
        self.browse(port)
        right = self.notified[0]
        code, hdrs, body = self.login(port, right)
        self.assertEqual((code, body), (200, {"ok": True}))
        cookie = hdrs["Set-Cookie"]
        for part in ("HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=31536000"):
            self.assertIn(part, cookie)
        self.assertNotIn("Secure", cookie)                      # plain http on 127.0.0.1
        self.assertNotIn(self.TOK, cookie)                      # its own secret, not the page token
        dev = self.device_cookie(hdrs)
        self.assertGreaterEqual(len(dev), 40)
        # the device cookie opens the page and the API
        c = {"Cookie": f"meeting_copilot_device={dev}"}
        self.assertEqual(self.req(port, "/", c)[0], 200)
        self.assertEqual(self.req(port, "/state", c)[0], 200)
        self.assertEqual(self.req(port, "/mute", {**c, "Content-Type": "application/json", "Origin": self.ORIGIN},
                                  "POST", b'{"kind":"x"}')[0], 200)
        self.assertEqual(self.req(port, "/state", {"Cookie": "meeting_copilot_device=wrong"})[0], 401)
        self.assertEqual(self.req(port, "/state", {"Authorization": f"Bearer {dev}"})[0], 401)  # not a token
        # single use: the same code again is refused
        self.assertEqual(self.login(port, right)[0], 410)
        # the file has only a hash of the cookie, readable only by you
        path = os.path.join(self.t, "devices.json")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn(dev, text)
        self.assertIn(hashlib.sha256(dev.encode()).hexdigest(), text)
        if not WIN:
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        # `devices forget` logs every device out of the running copilot
        self.assertEqual(copilot.Devices(copilot.Path(path)).forget_all(), 1)
        self.assertEqual(self.req(port, "/state", c)[0], 401)

    def test_expired_code_is_refused(self):
        port = self.serve()
        self.browse(port)
        right = self.notified[0]
        self.clock.t += copilot.CODE_TTL_S + 1
        code, _, body = self.login(port, right)
        self.assertEqual(code, 410)
        self.assertIn("expired", body["error"])
        self.assertIsNone(copilot.PAIR.current())
        self.browse(port)                                        # a new view: a new code, a new notification
        self.assertEqual(len(self.notified), 2)

    def test_wrong_codes_use_up_the_code(self):
        port = self.serve()
        self.browse(port)
        right = self.notified[0]
        wrong = f"{(int(right) + 1) % 10 ** 6:06d}"
        for left in (4, 3, 2, 1):
            code, _, body = self.login(port, wrong)
            self.assertEqual((code, body["left"]), (401, left))
        code, _, body = self.login(port, "12ab")                 # the 5th wrong try ends the code
        self.assertEqual(code, 429)
        self.assertIn("new code", body["error"])
        self.assertEqual(self.login(port, right)[0], 410)        # even the right one, now
        self.assertIsNone(copilot.PAIR.current())

    def test_global_limit_per_minute(self):
        port = self.serve()
        for _ in range(2):                                       # two codes, 5 wrong tries each
            self.browse(port)
            right = self.notified[-1]
            for _ in range(5):
                self.login(port, f"{(int(right) + 1) % 10 ** 6:06d}")
        self.browse(port)
        right = self.notified[-1]
        self.assertEqual(len(self.notified), 3)
        code, hdrs, body = self.login(port, right)               # 10 wrong in a minute: even the right code waits
        self.assertEqual(code, 429)
        self.assertGreaterEqual(int(hdrs["Retry-After"]), 1)
        self.assertEqual(copilot.PAIR.current()[0], right)       # the limit does not burn the code
        self.clock.t += 61
        self.assertEqual(self.login(port, right)[0], 200)

    def test_code_compare_is_constant_time(self):
        calls = []
        real = copilot.hmac.compare_digest

        def spy(a, b):
            calls.append((a, b))
            return real(a, b)

        p = copilot.Pairing(clock=self.clock)
        code, _, _ = p.get_or_create()
        copilot.hmac.compare_digest = spy
        try:
            self.assertEqual(p.try_code("000000" if code != "000000" else "111111")[0], "wrong")
            self.assertEqual(p.try_code(code)[0], "ok")
        finally:
            copilot.hmac.compare_digest = real
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], (code.encode(), code.encode()))

    def test_one_active_code_and_codes_are_random(self):
        p = copilot.Pairing(clock=self.clock)
        a, left, made = p.get_or_create()
        self.assertEqual((left, made), (300, True))
        self.clock.t += 100
        self.assertEqual(p.get_or_create(), (a, 200, False))      # reused while valid
        self.assertEqual(p.current(), (a, 200))
        codes = {copilot.Pairing().get_or_create()[0] for _ in range(200)}
        self.assertGreater(len(codes), 150)
        self.assertTrue(all(re.match(r"^[0-9]{6}$", c) for c in codes))

    def test_login_post_passes_the_origin_and_host_checks(self):
        port = self.serve()
        self.browse(port)
        right = self.notified[0]
        self.assertEqual(self.login(port, right, origin="https://evil.example")[0], 403)
        self.assertEqual(self.login(port, right, host="evil.example:4390")[0], 403)
        code, _, body = self.req(port, "/login", {"Origin": self.ORIGIN, "Content-Type": "text/plain"}, "POST",
                                 json.dumps({"code": right}).encode())
        self.assertEqual((code, body), (403, b"origin"))
        self.assertEqual(self.req(port, "/login/code", {"Origin": "https://evil.example",
                                                        "Content-Type": "application/json"}, "POST", b"{}")[0], 403)
        self.assertEqual(copilot.PAIR.current()[0], right)       # refused tries never touch the code
        self.assertEqual(self.login(port, right)[0], 200)

    def test_lan_and_tunnel_logins(self):
        # the LAN listener: Origin must be the page itself; a public client never reaches the code
        self.use({"MEETING_COPILOT_LAN": "1", "MEETING_COPILOT_LAN_PORT": "8080"})
        port = self.serve(lan=True, client_ip="192.168.1.20")
        self.browse(port, host="copilot.local:8080")
        right = self.notified[-1]
        self.assertEqual(self.login(port, right, origin="http://evil.example", host="copilot.local:8080")[0], 403)
        code, hdrs, _ = self.login(port, right, origin="http://copilot.local:8080", host="copilot.local:8080")
        self.assertEqual(code, 200)
        self.assertNotIn("Secure", hdrs["Set-Cookie"])
        pub = self.serve(lan=True, client_ip="203.0.113.5")
        self.assertEqual(self.browse(pub, host="copilot.local:8080")[:3:2], (403, b"network"))
        # behind an https tunnel: the device cookie is Secure
        self.use({"MEETING_COPILOT_EXTRA_HOSTS": "copilot.example.com"})
        port = self.serve()
        self.browse(port, host="copilot.example.com")
        right = self.notified[-1]
        code, hdrs, _ = self.login(port, right, origin="https://copilot.example.com", host="copilot.example.com",
                                   extra={"X-Forwarded-Proto": "https"})
        self.assertEqual(code, 200)
        self.assertIn("; Secure", hdrs["Set-Cookie"])

    def test_send_the_code_again_never_returns_it(self):
        port = self.serve()
        o = {"Content-Type": "application/json", "Origin": self.ORIGIN}
        code, _, body = self.req(port, "/login/code", o, "POST", b"{}")
        self.assertEqual((code, json.loads(body)), (200, {"ok": True}))
        self.assertEqual(len(self.notified), 1)                  # no code yet: one made and shown
        self.req(port, "/login/code", o, "POST", b"{}")
        self.assertEqual(len(self.notified), 1)                  # again at once: not shown again (spam)
        self.clock.t += copilot.NOTIFY_EVERY_S
        self.req(port, "/login/code", o, "POST", b"{}")
        self.assertEqual(self.notified, [self.notified[0]] * 2)  # later: the same code shown again

    def test_bearer_token_and_token_url_still_work(self):
        port = self.serve()
        self.assertEqual(self.req(port, "/state", {"Authorization": f"Bearer {self.TOK}"})[0], 200)
        code, hdrs, _ = self.req(port, f"/?token={self.TOK}")
        self.assertEqual((code, hdrs["Location"]), (303, "/"))
        self.assertIn(f"meeting_copilot_token={self.TOK}", hdrs["Set-Cookie"])
        # a browser with a wrong ?token= gets the login page, not a dead end
        code, hdrs, body = self.browse(port, "/?token=wrong")
        self.assertEqual(code, 401)
        self.assertIn(b"6-digit code", body)

    def test_pair_endpoint_for_logged_in_pages(self):
        port = self.serve()
        auth = {"Authorization": f"Bearer {self.TOK}"}
        self.assertEqual(json.loads(self.req(port, "/pair", auth)[2]), {"code": None, "expires_in": 0})
        o = {**auth, "Content-Type": "application/json", "Origin": self.ORIGIN}
        got = json.loads(self.req(port, "/pair", o, "POST", b"{}")[2])
        self.assertRegex(got["code"], r"^[0-9]{6}$")
        self.assertEqual(got["expires_in"], 300)
        self.assertEqual(self.notified, [])                      # the person asking already sees it
        self.assertEqual(json.loads(self.req(port, "/pair", auth)[2])["code"], got["code"])
        self.assertEqual(self.login(port, got["code"])[0], 200)

    def test_one_time_login_link_for_the_mac_browser(self):
        port = self.serve()
        auth = {"Authorization": f"Bearer {self.TOK}"}
        link = json.loads(self.req(port, "/login-link", auth)[2])["path"]
        self.assertRegex(link, r"^/\?once=[\w-]{20,}$")
        self.assertNotIn(self.TOK, link)
        code, hdrs, _ = self.req(port, link)
        self.assertEqual((code, hdrs["Location"]), (303, "/"))
        self.assertIn(f"meeting_copilot_token={self.TOK}", hdrs["Set-Cookie"])
        self.assertEqual(self.req(port, link)[0], 401)           # single use
        link = json.loads(self.req(port, "/login-link", auth)[2])["path"]
        self.clock.t += copilot.LOGIN_LINK_TTL_S + 1
        self.assertEqual(self.req(port, link)[0], 401)           # expired
        # only the token itself may make one: not a paired device, not a browser with no login
        dev = copilot.DEVICES.add("test")
        self.assertEqual(self.req(port, "/login-link", {"Cookie": f"meeting_copilot_device={dev}"})[0], 403)
        self.assertEqual(self.req(port, "/login-link")[0], 401)

    def test_devices_file_rules(self):
        path = copilot.Path(self.t) / "d.json"
        d = copilot.Devices(path)
        self.assertFalse(d.ok("x"))
        self.assertFalse(d.ok(""))
        secrets_ = [d.add(f"dev {i}") for i in range(copilot.DEVICES_MAX + 3)]
        self.assertEqual(len(d.list()), copilot.DEVICES_MAX)     # the oldest are dropped
        self.assertFalse(d.ok(secrets_[0]))
        self.assertTrue(d.ok(secrets_[-1]))
        path.write_text("not json")                              # a broken file logs everyone out, no crash
        self.assertFalse(d.ok(secrets_[-1]))
        s = d.add("new")
        with open(path, encoding="utf-8") as f:
            items = json.load(f)["devices"]
        items[0]["created"] -= copilot.DEVICE_MAX_AGE_S + 10     # a year old: expired
        copilot._write_private(path, json.dumps({"devices": items}))
        self.assertFalse(d.ok(s))
        self.assertFalse(copilot.Devices(None).ok(s))
        # a label from a User-Agent never carries terminal escapes; a bad id in the file is skipped
        d.add("evil \x1b]0;title\x07 \u00e9")
        copilot._write_private(path, json.dumps({"devices": d.list() + [{"id": "\u00e9" * 64, "created": 1e12}]}))
        self.assertEqual([x["label"] for x in d.list()], ["evil ?]0;title? ?"])
        self.assertFalse(d.ok("x"))

    @unittest.skipIf(WIN, NO_BASH_WHY)
    def test_notification_runs_osascript_with_the_code_as_an_argument(self):
        exe, log = fake_osascript(self.t)
        copilot.notify = PairingCode.real_notify
        self.use({"MEETING_COPILOT_OSASCRIPT": exe})
        copilot.notify("123456")
        lines = wait_for(lambda: read_lines(log), 10)
        self.assertEqual(lines, ["-e|on run argv|-e|display notification (item 2 of argv) with title (item 1 of argv)"
                                 "|-e|end run|Meeting Copilot|Code to open the page: 123456 (valid 5 minutes)"])
        self.use({"MEETING_COPILOT_OSASCRIPT": "off"})
        self.assertIsNone(copilot.S.osascript)
        copilot.notify("654321")                                 # off: nothing runs
        time.sleep(0.5)
        self.assertEqual(len(read_lines(log)), 1)
        if sys.platform != "darwin":
            self.assertIsNone(copilot.Settings({}).osascript)    # a no-op off macOS


PairingCode.real_notify = staticmethod(copilot.notify)


class Seed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = os.path.realpath(self.tmp.name)
        self.home = os.path.join(self.t, "home")
        os.makedirs(self.home)

    def tearDown(self):
        for root, dirs, _ in os.walk(self.t):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o700)
        self.tmp.cleanup()

    def env(self, **extra):
        """A clean environment: fake HOME, no XDG_CONFIG_HOME, no MEETING_COPILOT_*, no API keys."""
        drop = ("MEETING_COPILOT", "XDG_CONFIG_HOME", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY")
        env = {k: v for k, v in os.environ.items() if not k.startswith(drop)}
        env.update(HOME=self.home, USERPROFILE=self.home, **extra)
        return env

    def get(self, env, key="PORT"):
        return subprocess.run([PY, os.path.join(ROOT, "lib", "copilot.py"), "--get", key], env=env,
                              capture_output=True, text=True, timeout=30)

    def test_first_command_creates_the_file_under_home(self):
        p = self.get(self.env())
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "\n", ""))
        path = os.path.join(self.home, ".config", "meeting-copilot", "config")
        self.assertEqual(read_bytes(path), read_bytes(STARTER))

    def test_xdg_config_home_is_respected(self):
        xdg = os.path.join(self.t, "xdg", "not-there-yet")
        copilot.seed_config({"XDG_CONFIG_HOME": xdg})
        self.assertEqual(read_bytes(os.path.join(xdg, "meeting-copilot", "config")), read_bytes(STARTER))

    def test_meeting_copilot_config_is_respected(self):
        xdg = os.path.join(self.t, "xdg")
        mine = os.path.join(self.t, "sub", "my-config")
        self.assertEqual(self.get(self.env(XDG_CONFIG_HOME=xdg, MEETING_COPILOT_CONFIG=mine)).returncode, 0)
        self.assertEqual(read_bytes(mine), read_bytes(STARTER))
        self.assertFalse(os.path.exists(xdg))

    def test_existing_file_is_never_changed(self):
        for content in (b"MEETING_COPILOT_PORT=4555\n", b"", b"garbage \xff\xfe\r\n"):
            with self.subTest(content=content):
                path = os.path.join(self.t, "config")
                with open(path, "wb") as f:
                    f.write(content)
                copy = os.path.join(self.t, "config.before")
                with open(copy, "wb") as f:
                    f.write(content)
                before = os.stat(path).st_mtime_ns
                copilot.seed_config({"MEETING_COPILOT_CONFIG": path})
                self.get(self.env(MEETING_COPILOT_CONFIG=path))
                self.assertTrue(filecmp.cmp(path, copy, shallow=False))
                self.assertEqual(os.stat(path).st_mtime_ns, before)

    def test_cannot_create_no_crash(self):
        blocker = os.path.join(self.t, "a-file")
        with open(blocker, "w") as f:
            f.write("x")
        copilot.seed_config({"XDG_CONFIG_HOME": blocker})     # folder inside a regular file: fails on every OS
        p = self.get(self.env(MEETING_COPILOT_CONFIG=os.path.join(blocker, "config")), "MODEL")
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "\n", ""))

    @unittest.skipIf(WIN or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "read-only folders need POSIX permissions and a non-root user")
    def test_read_only_folder_no_crash(self):
        ro = os.path.join(self.t, "ro")
        os.makedirs(ro)
        os.chmod(ro, 0o500)
        p = self.get(self.env(XDG_CONFIG_HOME=ro))
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "\n", ""))
        self.assertFalse(os.path.exists(os.path.join(ro, "meeting-copilot")))

    def test_relative_path_is_skipped(self):
        cwd = os.path.join(self.t, "cwd")
        os.makedirs(cwd)
        p = subprocess.run([PY, os.path.join(ROOT, "lib", "copilot.py"), "--get", "PORT"], cwd=cwd,
                           env=self.env(MEETING_COPILOT_CONFIG="relative-config"), capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(os.listdir(cwd), [])

    def test_seeded_defaults_behave_like_no_file(self):
        none = {"XDG_CONFIG_HOME": os.path.join(self.t, "none")}
        seeded = {"XDG_CONFIG_HOME": os.path.join(self.t, "seeded")}
        copilot.seed_config(seeded)
        self.assertTrue(os.path.isfile(os.path.join(self.t, "seeded", "meeting-copilot", "config")))
        self.assertEqual(copilot.load_config(seeded), copilot.load_config(none))
        self.assertEqual(copilot.load_config(seeded), {})
        self.assertEqual(vars(copilot.Settings(copilot.load_config(seeded))),
                         vars(copilot.Settings(copilot.load_config(none))))
        # what bin/meeting-copilot reads through --get; "no file" sits inside a regular file, so the
        # seed cannot create it there either
        blocker = os.path.join(self.t, "a-file")
        with open(blocker, "w") as f:
            f.write("x")
        no_file = self.env(MEETING_COPILOT_CONFIG=os.path.join(blocker, "config"))
        with_file = self.env(MEETING_COPILOT_CONFIG=os.path.join(self.t, "seeded", "meeting-copilot", "config"))
        for key in ("PORT", "DATA_DIR", "OPEN_CMD", "MODEL", "DB"):
            self.assertEqual(self.get(with_file, key).stdout, self.get(no_file, key).stdout)
        self.assertFalse(os.path.exists(os.path.join(self.t, "none")))

    def test_starter_lists_every_key_commented_out(self):
        with open(os.path.join(ROOT, "lib", "copilot.py"), encoding="utf-8") as f:
            code = f.read()
        keys = set(re.findall(r'g\("([A-Z_]+)"', code)) | {"OPEN_CMD"}
        self.assertGreater(len(keys), 15)
        with open(STARTER, encoding="utf-8") as f:
            text = f.read()
        for k in sorted(keys):
            self.assertIn(f"\n#MEETING_COPILOT_{k}=", text, k)
        active = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        self.assertEqual(active, [])

    @unittest.skipIf(WIN, NO_BASH_WHY)
    def test_launcher_first_use_creates_it(self):
        env = self.env(MEETING_COPILOT_PYTHON=PY, XDG_DATA_HOME=os.path.join(self.t, "data"))
        p = subprocess.run(["bash", os.path.join(ROOT, "bin", "meeting-copilot"), "status"], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.stdout.strip(), "not running")
        path = os.path.join(self.home, ".config", "meeting-copilot", "config")
        self.assertEqual(read_bytes(path), read_bytes(STARTER))

# ------------------------------------------------------------------ the real program, as a process


class Live(unittest.TestCase):
    """Starts lib/copilot.py against a fake Muesli database."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.t = os.path.realpath(self.tmp.name)
        self.home = os.path.join(t, "home")
        os.makedirs(self.home)
        self.db = os.path.join(t, "muesli.db")
        self.sessions = os.path.join(t, "sessions")
        self.claude_log = os.path.join(t, "claude.jsonl")
        self.port = free_port()
        self.fakebin = os.path.join(t, "fakebin")
        os.makedirs(self.fakebin)
        with open(os.path.join(self.fakebin, "claude"), "w") as f:
            f.write(f'#!/bin/sh\nexec "{PY}" "{ROOT}/tests/fake_claude.py" "$@"\n')
        os.chmod(os.path.join(self.fakebin, "claude"), 0o755)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("MEETING_COPILOT", "CLAUDE"))}
        self.env.update(HOME=self.home, USERPROFILE=self.home, XDG_DATA_HOME=os.path.join(t, "data"),
                        PATH=self.fakebin + os.pathsep + self.env.get("PATH", ""),
                        FAKE_CLAUDE_LOG=self.claude_log,
                        MEETING_COPILOT_CONFIG=os.path.join(t, "no-config"),
                        MEETING_COPILOT_API_CREDIT="off",   # never an API-credit wrapper installed on this machine
                        MEETING_COPILOT_DB=self.db, MEETING_COPILOT_PORT=str(self.port),
                        MEETING_COPILOT_SESSIONS_DIR=self.sessions,
                        MEETING_COPILOT_POLL_S="0.2", MEETING_COPILOT_ASSIST_S="0.5",
                        MEETING_COPILOT_DATA_S="0.5", PYTHONIOENCODING="utf-8",
                        MEETING_COPILOT_OSASCRIPT="off")   # never a real notification on the test machine
        self.proc = None
        self.out = os.path.join(t, "copilot.log")
        fake_meeting.prepare(copilot.Path(self.db))

    def tearDown(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        for _ in range(50):
            try:
                self.tmp.cleanup()
                return
            except OSError:
                time.sleep(0.2)

    def start(self, **extra):
        self.env.update(extra)
        self.logf = open(self.out, "w")
        self.addCleanup(self.logf.close)
        self.proc = subprocess.Popen([PY, "-u", os.path.join(ROOT, "lib", "copilot.py")], env=self.env,
                                     stdout=self.logf, stderr=subprocess.STDOUT)
        self.assertTrue(wait_for(lambda: self._try_state(), 15), self.log())

    def log(self):
        with open(self.out, encoding="utf-8", errors="replace") as f:
            return f.read()

    def _try_state(self):
        try:
            return self.state()
        except (urllib.error.URLError, ConnectionError, OSError):
            return None

    def token(self):
        """The page token the copilot keeps in its data folder (created on first start)."""
        try:
            with open(os.path.join(self.t, "data", "meeting-copilot", "token"), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def request(self, path, data=None, headers=None, auth=True):
        # auth=True sends the token cookie, as the browser does after opening the printed URL once
        h = dict(headers or {})
        if auth and "Cookie" not in h:
            h["Cookie"] = f"meeting_copilot_token={self.token()}"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=h)
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=3) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def raw(self, path, headers=None, method="GET", body=None):
        """(status, headers, body) without following redirects or adding the cookie."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            c.request(method, path, body=body, headers=headers or {})
            r = c.getresponse()
            return r.status, r.headers, r.read()
        finally:
            c.close()

    def state(self):
        code, body = self.request("/state")
        return json.loads(body) if code == 200 else None

    def post(self, path, body, origin=None):
        h = {"Content-Type": "application/json"}
        h["Origin"] = origin or f"http://127.0.0.1:{self.port}"
        return self.request(path, json.dumps(body).encode(), h)[0]

    def claude_calls(self):
        if not os.path.exists(self.claude_log):
            return []
        with open(self.claude_log, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]

    def speak(self):
        fake_meeting.speak(copilot.Path(self.db), 0)

    # -------------------------------------------------------------- with the fake claude

    @unittest.skipIf(WIN, NO_FAKE_CLAUDE_WHY)
    def test_suggestions_corrections_goals_and_facts(self):
        notes, wiki = os.path.join(self.t, "notes"), os.path.join(self.t, "wiki")
        os.makedirs(notes)
        os.makedirs(wiki)
        with open(os.path.join(notes, "contracts.md"), "w") as f:
            f.write("Cleaning contract ends 2027-03-31.\n")
        mcp = os.path.join(self.t, "mcp.json")
        with open(mcp, "w") as f:
            json.dump({"mcpServers": {"chat": {"command": "true"}}}, f)
        self.start(MEETING_COPILOT_SEARCH_DIRS=notes + os.pathsep + wiki,
                   MEETING_COPILOT_MCP_CONFIG=mcp, MEETING_COPILOT_MCP_TOOLS="mcp__chat__history")
        self.assertEqual(self.post("/context", {"goal": "Keep the price flat", "goals": ["Price", "End date", ""]}), 200)
        self.speak()
        st = wait_for(lambda: (lambda s: s if s and s["say"] and s["data"] and s["corrections"] else None)(self.state()))
        self.assertTrue(st, self.log())
        self.assertTrue(st["live"])
        self.assertEqual(st["meeting"]["title"], fake_meeting.TITLE)
        self.assertIn("fifteen percent", st["say"][0]["text"])
        self.assertEqual(st["ask"][0]["text"][:3], "Who")
        self.assertEqual(st["corrections"][0]["quote"], "someone will sort it out")
        self.assertIn("2027-03-31", st["data"][0]["markdown"])
        self.assertEqual([g["covered"] for g in st["goals"]], [True, False])  # kept across meeting start
        self.assertEqual(st["metrics"]["questions"], 0)

        calls = self.claude_calls()
        assist = [c for c in calls if "--json-schema" in c["argv"]]
        data = [c for c in calls if "--json-schema" not in c["argv"]]
        self.assertTrue(assist and data)
        a = assist[0]["argv"]
        self.assertEqual(a[a.index("--tools") + 1], "")                 # suggestions: no tools at all
        self.assertIn("--strict-mcp-config", a)
        self.assertNotIn("--mcp-config", a)
        self.assertIn("## NEW", assist[0]["stdin"])
        self.assertIn("Keep the price flat", assist[0]["stdin"])
        d = data[0]["argv"]
        self.assertEqual(d[d.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(d[d.index("--mcp-config") + 1], os.path.realpath(mcp))
        self.assertEqual(d[d.index("--allowedTools") + 1],
                         ",".join(f"{t}(/{x}/**)" for t in ("Read", "Grep", "Glob") for x in (notes, wiki))
                         + ",mcp__chat__history")
        self.assertEqual([d[i + 1] for i, v in enumerate(d) if v == "--add-dir"],
                         [os.path.realpath(notes), os.path.realpath(wiki)])
        self.assertNotIn(os.path.realpath(data[0]["cwd"]), (os.path.realpath(notes), os.path.realpath(wiki)))
        self.assertIn(notes, d[d.index("--append-system-prompt") + 1])
        # nothing repeats when the same answers come back
        time.sleep(2)
        st2 = self.state()
        self.assertEqual(len(st2["say"]), 1)
        self.assertEqual(len(st2["data"]), 1)

    @unittest.skipIf(WIN, NO_FAKE_CLAUDE_WHY)
    def test_without_search_or_mcp_only_suggestions(self):
        self.start()
        self.speak()
        self.assertTrue(wait_for(lambda: (self.state() or {}).get("say")), self.log())
        time.sleep(2)
        st = self.state()
        self.assertFalse(st["data_enabled"])
        self.assertEqual(st["data"], [])
        calls = self.claude_calls()
        self.assertTrue(calls)
        self.assertTrue(all("--json-schema" in c["argv"] for c in calls))
        self.assertIn("fact search is off", self.log())

    @unittest.skipIf(WIN, NO_FAKE_CLAUDE_WHY)
    def test_context_file_and_language_go_to_claude(self):
        ctx = os.path.join(self.t, "me.md")
        with open(ctx, "w", encoding="utf-8") as f:
            f.write("The user is Ana; WhatsApp sessions: ana, shop.\n")
        self.start(MEETING_COPILOT_CONTEXT_FILE=ctx, MEETING_COPILOT_LANGUAGE="Spanish")
        self.speak()
        assist = lambda: [c for c in self.claude_calls() if "--json-schema" in c["argv"]]  # noqa: E731
        self.assertTrue(wait_for(assist), self.log())
        a = assist()[0]["argv"]
        prompt = a[a.index("--append-system-prompt") + 1]
        self.assertIn("WhatsApp sessions: ana, shop.", prompt)
        self.assertIn("Write every item in Spanish", prompt)

    @unittest.skipIf(WIN, NO_FAKE_CLAUDE_WHY)
    def test_prompt_file_goes_to_claude_and_suggestions_still_parse(self):
        pf = os.path.join(self.t, "prompt.md")
        with open(pf, "w", encoding="utf-8") as f:
            f.write("MY PROMPT: give say and ask in {language}.\n")
        self.start(MEETING_COPILOT_PROMPT_FILE=pf, MEETING_COPILOT_LANGUAGE="Spanish")
        self.speak()
        self.assertTrue(wait_for(lambda: (self.state() or {}).get("say")), self.log())
        a = [c for c in self.claude_calls() if "--json-schema" in c["argv"]][0]["argv"]
        prompt = a[a.index("--append-system-prompt") + 1]
        self.assertEqual(prompt, "MY PROMPT: give say and ask in Spanish.")
        self.assertNotIn("live meeting copilot", prompt)
        self.assertNotIn("note: prompt file", self.log())

    @unittest.skipIf(WIN, NO_FAKE_CLAUDE_WHY)
    def test_missing_prompt_file_logs_one_line_and_uses_the_built_in(self):
        self.start(MEETING_COPILOT_PROMPT_FILE=os.path.join(self.t, "nope.md"))
        self.speak()
        self.assertTrue(wait_for(lambda: (self.state() or {}).get("say")), self.log())
        self.assertEqual(self.log().count("note: prompt file not found or unreadable"), 1, self.log())
        a = [c for c in self.claude_calls() if "--json-schema" in c["argv"]][0]["argv"]
        self.assertEqual(a[a.index("--append-system-prompt") + 1], copilot.PROMPT_ASSIST)

    @unittest.skipIf(WIN, NO_FAKE_CLAUDE_WHY)
    def test_data_prompt_file_goes_to_claude_and_facts_still_show(self):
        notes = os.path.join(self.t, "notes")
        os.makedirs(notes)
        pf = os.path.join(self.t, "data.md")
        with open(pf, "w", encoding="utf-8") as f:
            f.write("MY DATA PROMPT. Search {sources}. Answer EMPTY when nothing.\n")
        self.start(MEETING_COPILOT_DATA_PROMPT_FILE=pf, MEETING_COPILOT_SEARCH_DIRS=notes)
        self.speak()
        self.assertTrue(wait_for(lambda: (self.state() or {}).get("data")), self.log())
        a = [c for c in self.claude_calls() if "--json-schema" not in c["argv"]][0]["argv"]
        prompt = a[a.index("--append-system-prompt") + 1]
        self.assertTrue(prompt.startswith("MY DATA PROMPT. Search - search the user's notes in these folders"))
        self.assertIn("2027-03-31", self.state()["data"][0]["markdown"])
        s = [c for c in self.claude_calls() if "--json-schema" in c["argv"]][0]["argv"]
        self.assertEqual(s[s.index("--append-system-prompt") + 1], copilot.PROMPT_ASSIST, "suggestions untouched")
        self.assertNotIn("note: data prompt file", self.log())

    @unittest.skipIf(WIN, NO_FAKE_CLAUDE_WHY)
    def test_missing_data_prompt_file_logs_one_line_and_uses_the_built_in(self):
        notes = os.path.join(self.t, "notes")
        os.makedirs(notes)
        self.start(MEETING_COPILOT_DATA_PROMPT_FILE=os.path.join(self.t, "nope.md"), MEETING_COPILOT_SEARCH_DIRS=notes)
        self.speak()
        self.assertTrue(wait_for(lambda: (self.state() or {}).get("data")), self.log())
        self.assertEqual(self.log().count("note: data prompt file not found or unreadable"), 1, self.log())
        a = [c for c in self.claude_calls() if "--json-schema" not in c["argv"]][0]["argv"]
        self.assertIn("You are a silent fact assistant", a[a.index("--append-system-prompt") + 1])

    @unittest.skipIf(WIN, "SIGTERM cannot be delivered as a signal on Windows")
    def test_sigterm_saves_session(self):
        self.start(MEETING_COPILOT_CLAUDE=os.path.join(self.t, "no-claude"))
        self.speak()
        self.assertTrue(wait_for(lambda: (self.state() or {}).get("lines")))
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(10), 0, self.log())
        files = os.listdir(self.sessions)
        self.assertEqual(len(files), 1)

    # -------------------------------------------------------------- no claude at all (all systems)

    def test_without_claude_metrics_warnings_and_readonly_db(self):
        self.start(MEETING_COPILOT_CLAUDE=os.path.join(self.t, "no-claude"))
        self.speak()
        before = sha(self.db)
        st = wait_for(lambda: (lambda s: s if s and s["warnings"] and s["metrics"].get("minutes") == 2.2 else None)(self.state()))
        self.assertTrue(st, self.log())
        self.assertEqual(st["warnings"][0]["kind"], "talks_too_much")
        self.assertEqual(st["metrics"]["you_pct_5min"], 71)
        self.assertEqual(len(st["lines"]), 8)  # the page gets the last 8 lines
        self.assertTrue(any("claude command not found" in e for e in st["errors"]))
        self.assertEqual(st["say"], [])
        self.assertEqual(self.claude_calls(), [])
        time.sleep(1)
        self.assertEqual(sha(self.db), before)  # read-only: the database never changes
        self.assertIsNone(self.proc.poll())

    def test_http_guards_context_and_mute(self):
        self.start(MEETING_COPILOT_CLAUDE=os.path.join(self.t, "no-claude"))
        code, body = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Meeting Copilot", body)
        self.assertEqual(self.request("/state", headers={"Host": "evil.example:80"})[0], 403)
        self.assertEqual(self.post("/context", {"goal": "x"}, origin="https://evil.example"), 403)
        self.assertEqual(self.request("/context", b'{"goal":"x"}', {"Content-Type": "text/plain",
                         "Origin": f"http://127.0.0.1:{self.port}"})[0], 403)
        self.assertEqual(self.request("/context", b"not json", {"Content-Type": "application/json",
                         "Origin": f"http://127.0.0.1:{self.port}"})[0], 400)
        self.assertEqual(self.post("/context", {"goal": "Agree on the date", "goals": ["a", " ", "b"]}), 200)
        self.assertEqual(self.post("/mute", {"kind": "fast"}), 200)
        self.assertEqual(self.post("/nope", {}), 404)
        # a refused POST still reads the body first: closing with unread bytes resets the connection
        # and the client never gets the answer (seen on macOS CI)
        big = json.dumps({"goal": "x" * 60_000}).encode()
        for h, code, msg in (({"Origin": "https://evil.example"}, 403, b"origin"),
                             ({"Origin": f"http://127.0.0.1:{self.port}", "Cookie": "meeting_copilot_token=no"},
                              401, b"token")):
            for _ in range(100):
                r = self.raw("/context", dict(h, **{"Content-Type": "application/json"}), "POST", big)
                self.assertEqual(r[0], code)
                self.assertIn(msg, r[2])
        st = self.state()
        self.assertEqual(st["goal"], "Agree on the date")
        self.assertEqual([g["text"] for g in st["goals"]], ["a", "b"])
        self.assertEqual(st["status"], "live")  # setUp started a recording meeting

    def test_every_endpoint_needs_the_token(self):
        self.start(MEETING_COPILOT_CLAUDE=os.path.join(self.t, "no-claude"))
        tok = self.token()
        self.assertGreaterEqual(len(tok), 32)
        if not WIN:
            path = os.path.join(self.t, "data", "meeting-copilot", "token")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        o = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"}
        # no token, a wrong cookie, a wrong ?token: refused with a short plain message, nothing leaks
        for path in ("/", "/state", "/nope"):
            code, body = self.request(path, auth=False)
            self.assertEqual(code, 401, path)
            self.assertIn(b"token", body)
            self.assertNotIn(b"Meeting Copilot", body)
            self.assertEqual(self.request(path, headers={"Cookie": "meeting_copilot_token=wrong"})[0], 401)
            self.assertEqual(self.request(path + "?token=wrong", auth=False)[0], 401)
        for path in ("/context", "/mute", "/ai"):
            self.assertEqual(self.request(path, b'{"goal":"x"}', dict(o), auth=False)[0], 401, path)
            self.assertEqual(self.request(path + "?token=wrong", b'{"goal":"x"}', dict(o), auth=False)[0], 401)
        self.assertEqual(self.state()["goal"], "")
        # ?token=<right> sets an HttpOnly SameSite=Strict cookie and redirects to the clean URL
        code, hdrs, _ = self.raw(f"/?token={tok}")
        self.assertEqual(code, 303)
        self.assertEqual(hdrs["Location"], "/")
        cookie = hdrs["Set-Cookie"]
        self.assertIn(f"meeting_copilot_token={tok}", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        code, hdrs, _ = self.raw(f"/state?token={tok}")
        self.assertEqual((code, hdrs["Location"]), (303, "/state"))
        # the cookie opens everything; a program can also send the token as a bearer header
        code, body = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Meeting Copilot", body)
        self.assertEqual(self.post("/context", {"goal": "With cookie"}), 200)
        code, _, body = self.raw("/state", {"Authorization": f"Bearer {tok}"})
        self.assertEqual(json.loads(body)["goal"], "With cookie")
        self.assertEqual(self.raw("/state", {"Authorization": "Bearer wrong"})[0], 401)
        # a cookie jar with other cookies too
        self.assertEqual(self.request("/state", headers={"Cookie": f"a=b; meeting_copilot_token={tok}; c=d"})[0],
                         200)
        # the host check still comes first
        self.assertEqual(self.request("/state", headers={"Host": "evil.example:80"})[0], 403)
        # the same token after a restart: a page left open keeps working
        self.proc.kill()
        self.proc.wait()
        self.start()
        self.assertEqual(self.token(), tok)

    def set_status(self, status, meeting_id=None):
        con = sqlite3.connect(self.db)
        if meeting_id is None:
            con.execute("INSERT INTO meetings (title, start_time, meeting_status) VALUES (?, ?, ?)",
                        (fake_meeting.TITLE, fake_meeting.now_iso(), status))
        else:
            con.execute("UPDATE meetings SET meeting_status = ? WHERE id = ?", (status, meeting_id))
        con.commit()
        con.close()

    def calls_settle(self):
        """Waits until no AI call is still landing, then returns how many there were."""
        n = -1
        while n != len(self.claude_calls()):
            n = len(self.claude_calls())
            time.sleep(1.5)
        return n

    @unittest.skipIf(WIN, NO_FAKE_CLAUDE_WHY)
    def test_confirm_ai_waits_for_the_click_and_each_meeting_asks_again(self):
        self.start(MEETING_COPILOT_CONFIRM_AI="1")
        self.speak()
        self.assertTrue(wait_for(lambda: len((self.state() or {}).get("lines", [])) == 8), self.log())
        time.sleep(2)                                    # several rounds would have run by now
        st = self.state()
        mid = st["meeting"]["id"]
        self.assertTrue(st["confirm_ai"])
        self.assertFalse(st["ai_on"])
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(st["say"], [])
        self.assertTrue(st["metrics"])                   # transcript and metrics still work
        self.assertEqual(self.post("/ai", {"on": True, "meeting": mid}, origin="https://evil.example"), 403)
        self.assertEqual(self.post("/ai", {"on": "yes", "meeting": mid}), 200)   # only a real true turns it on
        self.assertFalse(self.state()["ai_on"])
        self.assertEqual(self.post("/ai", {"on": True, "meeting": mid + 1}), 409)  # page showed another meeting
        self.assertEqual(self.post("/ai", {"on": True, "meeting": None}), 409)     # page thought nothing was live
        self.assertFalse(self.state()["ai_on"])
        self.assertEqual(self.post("/ai", {"on": True, "meeting": mid}), 200)
        self.assertTrue(self.state()["ai_on"])
        self.assertTrue(wait_for(lambda: (self.state() or {}).get("say")), self.log())
        self.assertTrue(self.claude_calls())
        # Muesli leaves 'recording' for a moment and comes back with the same meeting: AI stays on
        self.set_status("paused", mid)
        self.assertTrue(wait_for(lambda: (lambda s: s and not s["live"])(self.state())), self.log())
        self.assertFalse(self.state()["ai_on"])
        self.set_status("recording", mid)
        self.assertTrue(wait_for(lambda: (self.state() or {}).get("live")), self.log())
        self.assertTrue(self.state()["ai_on"])
        # the meeting ends and a new one starts: it asks again
        fake_meeting.end(copilot.Path(self.db))
        self.assertTrue(wait_for(lambda: (lambda s: s and not s["live"])(self.state())), self.log())
        self.set_status("recording")
        self.assertTrue(wait_for(lambda: (self.state() or {}).get("live")), self.log())
        self.assertFalse(self.state()["ai_on"])
        n = self.calls_settle()
        self.speak()
        self.assertTrue(wait_for(lambda: len((self.state() or {}).get("lines", [])) == 8), self.log())
        time.sleep(2)
        self.assertEqual(len(self.claude_calls()), n)

    @unittest.skipIf(WIN, NO_FAKE_CLAUDE_WHY)
    def test_confirm_ai_stop_drops_the_answer_still_running(self):
        self.start(MEETING_COPILOT_CONFIRM_AI="1", FAKE_CLAUDE_SLEEP="3")
        mid = self.state()["meeting"]["id"]
        self.speak()
        self.assertEqual(self.post("/ai", {"on": True, "meeting": mid}), 200)
        self.assertTrue(wait_for(lambda: self.claude_calls()), self.log())   # a call is running
        self.assertEqual(self.post("/ai", {"on": False}), 200)
        self.assertFalse(self.state()["ai_on"])
        self.assertTrue(wait_for(lambda: "AI answer dropped" in self.log(), 15), self.log())
        st = self.state()
        self.assertEqual(st["say"], [])
        self.assertEqual(st["data"], [])

    @unittest.skipIf(WIN, NO_FAKE_CLAUDE_WHY)
    def test_confirm_ai_click_before_the_meeting_counts_for_it(self):
        fake_meeting.end(copilot.Path(self.db))          # no meeting yet
        self.start(MEETING_COPILOT_CONFIRM_AI="1")
        self.assertEqual(self.post("/ai", {"on": True, "meeting": None}), 200)
        st = self.state()
        self.assertTrue(st["ai_next"])
        self.assertFalse(st["ai_on"])
        self.set_status("recording")
        self.assertTrue(wait_for(lambda: (self.state() or {}).get("live")), self.log())
        st = self.state()
        self.assertTrue(st["ai_on"])
        self.assertFalse(st["ai_next"])
        self.speak()
        self.assertTrue(wait_for(lambda: (self.state() or {}).get("say")), self.log())

    def test_without_confirm_ai_the_ai_is_on(self):
        self.start(MEETING_COPILOT_CLAUDE=os.path.join(self.t, "no-claude"))
        st = self.state()
        self.assertFalse(st["confirm_ai"])
        self.assertTrue(st["ai_on"])
        self.assertFalse(st["ai_next"])

    def test_meeting_end_saves_session(self):
        self.start(MEETING_COPILOT_CLAUDE=os.path.join(self.t, "no-claude"))
        self.speak()
        self.assertTrue(wait_for(lambda: len((self.state() or {}).get("lines", [])) == 8))
        fake_meeting.end(copilot.Path(self.db))
        st = wait_for(lambda: (lambda s: s if s and not s["live"] else None)(self.state()))
        self.assertTrue(st, self.log())
        self.assertIn("ended", st["status"])
        files = wait_for(lambda: os.listdir(self.sessions) if os.path.isdir(self.sessions) else None)
        self.assertEqual(len(files), 1, self.log())
        with open(os.path.join(self.sessions, files[0]), encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(len(saved["lines"]), len(fake_meeting.SCRIPT))

    def test_switching_straight_to_another_meeting_keeps_the_last_lines(self):
        self.start(MEETING_COPILOT_CLAUDE=os.path.join(self.t, "no-claude"))
        self.speak()
        self.assertTrue(wait_for(lambda: len((self.state() or {}).get("lines", [])) == 8))
        time.sleep(0.5)
        # in one go: the last line of meeting A lands, A ends and meeting B starts recording
        con = sqlite3.connect(self.db)
        a = fake_meeting.meeting_id(con)
        con.execute("INSERT INTO meeting_transcript_checkpoints (meeting_id, timestamp_label, speaker, "
                    "start_seconds, end_seconds, text) VALUES (?, '00:09:00', 'You', 900, 905, 'Last words of A.')",
                    (a,))
        con.execute("UPDATE meetings SET meeting_status = 'completed' WHERE id = ?", (a,))
        con.execute("INSERT INTO meetings (title, start_time, meeting_status) VALUES ('B', ?, 'recording')",
                    (fake_meeting.now_iso(),))
        con.commit()
        con.close()
        st = wait_for(lambda: (lambda s: s if s and s["meeting"] and s["meeting"]["id"] != a else None)(self.state()))
        self.assertTrue(st, self.log())
        files = wait_for(lambda: os.listdir(self.sessions) if os.path.isdir(self.sessions) else None)
        self.assertEqual(len(files), 1, self.log())
        with open(os.path.join(self.sessions, files[0]), encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["meeting"]["id"], a)
        self.assertEqual(saved["lines"][-1]["text"], "Last words of A.")
        self.assertEqual(len(saved["lines"]), len(fake_meeting.SCRIPT) + 1)

    def muesli_exports(self, meeting_id, title):
        """Does what Muesli's auto-export does after a meeting: renames it, writes the .md, logs the path."""
        folder = os.path.join(self.t, "vault", "reunioes")
        os.makedirs(folder, exist_ok=True)
        stem = f"{copilot.local_date(fake_meeting.now_iso())}-{copilot.muesli_slug(title)}"
        md = os.path.join(folder, stem + ".md")
        with open(md, "w", encoding="utf-8") as f:
            f.write("# transcript\n")
        con = sqlite3.connect(self.db)
        con.execute("UPDATE meetings SET title = ? WHERE id = ?", (title, meeting_id))
        con.commit()
        con.close()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(os.path.join(self.t, "meeting-markdown-export.log"), "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] exported: id={meeting_id} paths={md}, {md[:-3]}.pdf\n")
        return stem, md

    def md_files(self):
        return sorted(x for x in os.listdir(self.sessions) if x.endswith(".md")) if os.path.isdir(self.sessions) else []

    @unittest.skipIf(WIN, NO_FAKE_CLAUDE_WHY)
    def test_markdown_session_waits_for_muesli_export_and_takes_its_name(self):
        self.start(MEETING_COPILOT_SAVE_FORMAT="markdown")
        mid = self.state()["meeting"]["id"]
        self.speak()
        self.assertTrue(wait_for(lambda: (self.state() or {}).get("say")), self.log())
        fake_meeting.end(copilot.Path(self.db))
        self.assertTrue(wait_for(lambda: (lambda s: s and not s["live"])(self.state())), self.log())
        first = f"{copilot.local_date(fake_meeting.now_iso())}-{copilot.muesli_slug(fake_meeting.TITLE)}-copilot.md"
        self.assertTrue(wait_for(lambda: self.md_files() == [first]), self.log())   # on disk at once
        stem, md = self.muesli_exports(mid, "Renewal: final terms")
        self.assertTrue(wait_for(lambda: self.md_files() == [stem + "-copilot.md"]), self.log())  # renamed
        self.assertTrue(stem.endswith("-renewal-final-terms"))
        with open(os.path.join(self.sessions, stem + "-copilot.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("kind: ai-suggestions", text)
        self.assertIn("not facts", text)
        self.assertIn("# Renewal: final terms (copilot)", text)
        self.assertIn("fifteen percent", text)           # the suggestion
        self.assertIn("someone will sort it out", text)  # the correction
        self.assertIn(os.path.basename(md), text)        # link to the transcript
        self.assertNotIn("One more thing: is the window cleaning", text)  # no transcript copy
        self.assertEqual([x for x in os.listdir(self.sessions) if x.endswith(".json")], [])

    def test_markdown_session_without_muesli_export_after_the_wait(self):
        self.start(MEETING_COPILOT_CLAUDE=os.path.join(self.t, "no-claude"),
                   MEETING_COPILOT_SAVE_FORMAT="markdown", MEETING_COPILOT_MUESLI_WAIT_S="1")
        self.speak()
        self.assertTrue(wait_for(lambda: (self.state() or {}).get("lines")), self.log())
        fake_meeting.end(copilot.Path(self.db))
        self.assertTrue(wait_for(lambda: self.md_files()), self.log())
        self.assertEqual(self.md_files(), [f"{copilot.local_date(fake_meeting.now_iso())}-"
                                           f"{copilot.muesli_slug(fake_meeting.TITLE)}-copilot.md"])

    @unittest.skipIf(WIN, "SIGTERM cannot be delivered as a signal on Windows")
    def test_markdown_session_written_at_once_on_stop(self):
        self.start(MEETING_COPILOT_CLAUDE=os.path.join(self.t, "no-claude"), MEETING_COPILOT_SAVE_FORMAT="markdown")
        self.speak()
        self.assertTrue(wait_for(lambda: (self.state() or {}).get("lines")), self.log())
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(10), 0, self.log())
        self.assertEqual(len(self.md_files()), 1, self.log())

    def test_missing_db_exits_with_message(self):
        env = dict(self.env, MEETING_COPILOT_DB=os.path.join(self.t, "missing.db"))
        p = subprocess.run([PY, os.path.join(ROOT, "lib", "copilot.py")], env=env, capture_output=True,
                           text=True, timeout=20)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Muesli database not found", p.stderr)

# ------------------------------------------------------------------ helper script, launcher, installer


class Tools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = os.path.realpath(self.tmp.name)
        self.home = os.path.join(self.t, "home")
        os.makedirs(self.home)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("MEETING_COPILOT", "CLAUDE"))}
        self.env.update(HOME=self.home, USERPROFILE=self.home, XDG_DATA_HOME=os.path.join(self.t, "data"),
                        MEETING_COPILOT_OSASCRIPT="off")   # never a real notification on the test machine

    def tearDown(self):
        for _ in range(50):
            try:
                self.tmp.cleanup()
                return
            except OSError:
                time.sleep(0.2)

    token = Live.token   # the same data folder layout: <t>/data/meeting-copilot/token

    def test_fake_meeting_refuses_real_db_and_runs(self):
        real = os.path.join(self.home, "Library", "Application Support", "Muesli", "muesli.db")
        os.makedirs(os.path.dirname(real))
        sqlite3.connect(real).close()
        script = os.path.join(ROOT, "tools", "fake_meeting.py")
        p = subprocess.run([PY, script, "prepare", real], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("refused", p.stderr)
        db = os.path.join(self.t, "test.db")
        for args in (["prepare", db], ["speak", db, "0"], ["end", db]):
            p = subprocess.run([PY, script, *args], env=self.env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
        con = sqlite3.connect(db)
        self.assertEqual(con.execute("SELECT meeting_status FROM meetings").fetchone()[0], "completed")
        self.assertEqual(con.execute("SELECT count(*) FROM meeting_transcript_checkpoints").fetchone()[0],
                         len(fake_meeting.SCRIPT))
        con.close()

    def test_fake_meeting_never_deletes_a_file_without_force(self):
        script = os.path.join(ROOT, "tools", "fake_meeting.py")
        db = os.path.join(self.t, "test.db")
        with open(db, "w") as f:
            f.write("my data")
        p = subprocess.run([PY, script, "prepare", db], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("--force", p.stderr)
        with open(db) as f:
            self.assertEqual(f.read(), "my data")
        # the copy can never replace its own source, even with --force
        p = subprocess.run([PY, script, "prepare", db, "--from", db, "--force"], env=self.env,
                           capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("refused", p.stderr)
        with open(db) as f:
            self.assertEqual(f.read(), "my data")
        p = subprocess.run([PY, script, "prepare", db, "--force"], env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        con = sqlite3.connect(db)
        self.assertEqual(con.execute("SELECT meeting_status FROM meetings").fetchone()[0], "recording")
        con.close()

    def launcher_env(self, port):
        db = os.path.join(self.t, "muesli.db")
        if not os.path.exists(db):
            fake_meeting.prepare(copilot.Path(db))
        return dict(self.env, MEETING_COPILOT_DB=db, MEETING_COPILOT_PORT=str(port),
                    MEETING_COPILOT_CONFIG=os.path.join(self.t, "no-config"),
                    MEETING_COPILOT_CLAUDE=os.path.join(self.t, "no-claude"), MEETING_COPILOT_PYTHON=PY)

    @unittest.skipIf(WIN, NO_BASH_WHY)
    def test_launcher_never_trusts_a_pid_file_of_another_process(self):
        launcher = os.path.join(ROOT, "bin", "meeting-copilot")
        env = self.launcher_env(free_port())
        pidfile = os.path.join(self.t, "data", "meeting-copilot", "meeting-copilot.pid")
        os.makedirs(os.path.dirname(pidfile))
        other = subprocess.Popen(["sleep", "60"])   # took the number the copilot had before a reboot
        try:
            with open(pidfile, "w") as f:
                f.write(f"{other.pid}\n")
            p = subprocess.run(["bash", launcher, "status"], env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 1, p.stdout)
            p = subprocess.run(["bash", launcher, "stop"], env=env, capture_output=True, text=True, timeout=30)
            self.assertIn("was not running", p.stdout)
            self.assertIsNone(other.poll(), "stop killed a process that is not the copilot")
            with open(pidfile, "w") as f:
                f.write(f"{other.pid}\n")
            try:
                p = subprocess.run(["bash", launcher, "--no-open"], env=env, capture_output=True, text=True,
                                   timeout=30)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                with open(pidfile) as f:
                    pid = int(f.read())
                self.assertNotEqual(pid, other.pid)      # it really started
                # the copilot removes its own pid file when it exits, however it is stopped
                os.kill(pid, signal.SIGTERM)
                self.assertTrue(wait_for(lambda: not os.path.exists(pidfile), 10))
            finally:
                subprocess.run(["bash", launcher, "stop"], env=env, capture_output=True, text=True, timeout=30)
        finally:
            other.kill()
            other.wait()

    @unittest.skipIf(WIN, NO_BASH_WHY)
    def test_launcher_shows_the_port_really_in_use(self):
        launcher = os.path.join(ROOT, "bin", "meeting-copilot")
        first, second = free_port(), free_port()
        env = self.launcher_env(first)
        try:
            p = subprocess.run(["bash", launcher, "--no-open"], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            p = subprocess.run(["bash", launcher, "--no-open", "--port", str(second)], env=env,
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn(f"http://127.0.0.1:{first}", p.stdout)
            self.assertNotIn(f"http://127.0.0.1:{second}", p.stdout)
            p = subprocess.run(["bash", launcher, "status", "--port", str(second)], env=env,
                               capture_output=True, text=True)
            self.assertIn(f"http://127.0.0.1:{first}", p.stdout)
        finally:
            subprocess.run(["bash", launcher, "stop"], env=env, capture_output=True, text=True, timeout=30)

    @unittest.skipIf(WIN, NO_BASH_WHY)
    def test_install_dry_run_install_uninstall(self):
        inst = os.path.join(ROOT, "install.sh")
        dest = os.path.join(self.home, ".local", "bin", "meeting-copilot")
        p = subprocess.run(["bash", inst, "--dry-run"], env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("would run: ln -sfn", p.stdout)
        self.assertFalse(os.path.lexists(dest))
        p = subprocess.run(["bash", inst], env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(os.readlink(dest), os.path.join(ROOT, "bin", "meeting-copilot"))
        p = subprocess.run(["bash", inst, "--uninstall"], env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(os.path.lexists(dest))

    @unittest.skipIf(WIN, NO_BASH_WHY)
    def test_launcher_start_status_stop_through_symlink(self):
        db = os.path.join(self.t, "muesli.db")
        fake_meeting.prepare(copilot.Path(db))
        port = free_port()
        link = os.path.join(self.t, "meeting-copilot")
        os.symlink(os.path.join(ROOT, "bin", "meeting-copilot"), link)
        env = dict(self.env, MEETING_COPILOT_DB=db, MEETING_COPILOT_PORT=str(port),
                   MEETING_COPILOT_CONFIG=os.path.join(self.t, "no-config"),
                   MEETING_COPILOT_CLAUDE=os.path.join(self.t, "no-claude"), MEETING_COPILOT_PYTHON=PY)
        try:
            p = subprocess.run(["bash", link, "--no-open"], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            tok = self.token()
            self.assertIn(f"meeting-copilot on http://127.0.0.1:{port}/ - ", p.stdout)   # a clean URL
            self.assertNotIn(tok, p.stdout + p.stderr)
            self.assertNotIn("token", p.stdout)
            p = subprocess.run(["bash", link, "status"], env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout)
            self.assertIn(f"on http://127.0.0.1:{port}/\n", p.stdout)
            self.assertNotIn(tok, p.stdout + p.stderr)
            req = urllib.request.Request(f"http://127.0.0.1:{port}/state",
                                         headers={"Authorization": f"Bearer {tok}"})
            with urllib.request.urlopen(req, timeout=3) as r:
                self.assertTrue(json.loads(r.read())["live"])
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/state", timeout=3)
            self.assertEqual(cm.exception.code, 401)
        finally:
            p = subprocess.run(["bash", link, "stop"], env=env, capture_output=True, text=True, timeout=30)
        self.assertIn("stopped", p.stdout)
        p = subprocess.run(["bash", link, "status"], env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)

    @unittest.skipIf(WIN, NO_BASH_WHY)
    def test_launcher_lan_listener_mdns_and_status_urls(self):
        launcher = os.path.join(ROOT, "bin", "meeting-copilot")
        port, lan = free_port(), free_port()
        exe, log = fake_dns_sd(self.t)
        env = dict(self.launcher_env(port), MEETING_COPILOT_LAN="1", MEETING_COPILOT_LAN_PORT=str(lan),
                   MEETING_COPILOT_LAN_IP="192.168.50.7", MEETING_COPILOT_DNS_SD=exe,
                   MEETING_COPILOT_EXTRA_HOSTS="copilot.example.com")
        pidfile = os.path.join(self.t, "data", "meeting-copilot", "meeting-copilot.pid")
        try:
            p = subprocess.run(["bash", launcher, "--no-open"], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            tok = self.token()
            self.assertIn(f"  LAN:    http://copilot.local:{lan}/\n", p.stdout)
            self.assertNotIn(tok, p.stdout + p.stderr)
            p = subprocess.run(["bash", launcher, "status"], env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout)
            self.assertIn(f"on http://127.0.0.1:{port}/\n", p.stdout)
            self.assertIn(f"  LAN:    http://copilot.local:{lan}/\n", p.stdout)
            self.assertIn(f"  LAN IP: http://192.168.50.7:{lan}/\n", p.stdout)
            self.assertIn("  public: https://copilot.example.com/\n", p.stdout)
            self.assertNotIn(tok, p.stdout + p.stderr)
            self.assertNotIn("token", p.stdout)
            calls = wait_for(lambda: dns_sd_calls(log), 10)
            self.assertEqual(calls[0][1], f"-P copilot _http._tcp local {lan} copilot.local 192.168.50.7")
            child = calls[0][0]
            self.assertTrue(alive(child))
            # the LAN listener answers with the same token rules
            c = http.client.HTTPConnection("127.0.0.1", lan, timeout=3)
            c.request("GET", "/state", headers={"Host": f"copilot.local:{lan}", "Authorization": f"Bearer {tok}"})
            self.assertEqual(c.getresponse().status, 200)
            c.close()
            c = http.client.HTTPConnection("127.0.0.1", lan, timeout=3)
            c.request("GET", "/state", headers={"Host": f"copilot.local:{lan}"})
            self.assertEqual(c.getresponse().status, 401)
            c.close()
            with open(pidfile) as f:
                pid = int(f.read())
        finally:
            p = subprocess.run(["bash", launcher, "stop"], env=env, capture_output=True, text=True, timeout=30)
        self.assertIn("stopped", p.stdout)
        self.assertTrue(wait_for(lambda: not alive(pid), 10))
        self.assertTrue(wait_for(lambda: not alive(child), 10), "dns-sd outlived the copilot")

    @unittest.skipIf(WIN, NO_BASH_WHY)
    def test_status_without_lan_prints_only_the_local_url(self):
        launcher = os.path.join(ROOT, "bin", "meeting-copilot")
        port = free_port()
        env = self.launcher_env(port)
        try:
            p = subprocess.run(["bash", launcher, "--no-open"], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            p = subprocess.run(["bash", launcher, "status"], env=env, capture_output=True, text=True)
            self.assertEqual(p.stdout.strip().splitlines(),
                             [f"running (pid {p.stdout.split('pid ')[1].split(')')[0]}) on "
                              f"http://127.0.0.1:{port}/",
                              "  code:   none now (open the page on the other device, or run `meeting-copilot code`)",
                              "  paired devices: 0"])
        finally:
            subprocess.run(["bash", launcher, "stop"], env=env, capture_output=True, text=True, timeout=30)

    @unittest.skipIf(WIN, NO_BASH_WHY)
    def test_launcher_pairing_code_status_devices_and_one_time_link(self):
        launcher = os.path.join(ROOT, "bin", "meeting-copilot")
        port = free_port()
        exe, notes = fake_osascript(self.t)
        opened = os.path.join(self.t, "opened.txt")
        opener = os.path.join(self.t, "my-browser")
        with open(opener, "w") as f:
            f.write('#!/bin/sh\nprintf "%s" "$1" > "$OPENED"\n')
        os.chmod(opener, 0o755)
        env = dict(self.launcher_env(port), MEETING_COPILOT_OSASCRIPT=exe, MEETING_COPILOT_OPEN_CMD=opener,
                   OPENED=opened)

        def run(*args):
            return subprocess.run(["bash", launcher, *args], env=env, capture_output=True, text=True, timeout=30)

        def get(path, headers=None, method="GET", body=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                c.request(method, path, body=body, headers=headers or {})
                r = c.getresponse()
                return r.status, r.headers, r.read()
            finally:
                c.close()

        try:
            p = run()
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            tok = self.token()
            self.assertNotIn(tok, p.stdout + p.stderr)
            # the launcher opened a one-time link: it logs this browser in once, then is dead
            with open(opened) as f:
                link = f.read()
            self.assertRegex(link, rf"^http://127\.0\.0\.1:{port}/\?once=[\w-]{{20,}}$")
            code, hdrs, _ = get(link.split(str(port), 1)[1])
            self.assertEqual((code, hdrs["Location"]), (303, "/"))
            self.assertIn(f"meeting_copilot_token={tok}", hdrs["Set-Cookie"])
            self.assertEqual(get(link.split(str(port), 1)[1])[0], 401)
            # a phone opens the page: login page, and the code shows on the Mac and in `status`
            code, _, body = get("/", {"Accept": "text/html"})
            self.assertEqual(code, 401)
            self.assertIn(b"6-digit code", body)
            got = wait_for(lambda: read_lines(notes), 10)
            pair = re.search(r"Code to open the page: ([0-9]{6})", got[0]).group(1)
            p = run("status")
            self.assertIn(f"  code:   {pair}  (pairing code, valid 5 min more)", p.stdout)
            self.assertIn("  paired devices: 0", p.stdout)
            self.assertNotIn(tok, p.stdout + p.stderr)
            # `code` prints the same active code
            p = run("code")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn(f"pairing code: {pair}", p.stdout)
            # the phone types it
            o = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"}
            code, hdrs, _ = get("/login", o, "POST", json.dumps({"code": pair}).encode())
            self.assertEqual(code, 200)
            dev = re.match(r"meeting_copilot_device=([^;]+)", hdrs["Set-Cookie"]).group(1)
            self.assertEqual(get("/state", {"Cookie": f"meeting_copilot_device={dev}"})[0], 200)
            p = run("status")
            self.assertIn("  code:   none now", p.stdout)
            self.assertIn("  paired devices: 1", p.stdout)
            p = run("devices")
            self.assertIn("1 paired device(s)", p.stdout)
            self.assertIn("via 127.0.0.1", p.stdout)
            p = run("devices", "forget")
            self.assertIn("1 paired device(s) logged out", p.stdout)
            self.assertEqual(get("/state", {"Cookie": f"meeting_copilot_device={dev}"})[0], 401)
            self.assertEqual(get("/state", {"Authorization": f"Bearer {tok}"})[0], 200)   # scripts unchanged
        finally:
            run("stop")
        p = run("code")
        self.assertEqual(p.returncode, 1)

    @unittest.skipIf(WIN, NO_BASH_WHY)
    def test_launcher_port_from_config_file_and_open_cmd(self):
        db = os.path.join(self.t, "muesli.db")
        fake_meeting.prepare(copilot.Path(db))
        port = free_port()
        opened = os.path.join(self.t, "opened.txt")
        opener = os.path.join(self.t, "my-browser")
        with open(opener, "w") as f:
            f.write('#!/bin/sh\nprintf "%s|" "$@" > "$OPENED"\n')
        os.chmod(opener, 0o755)
        cfg_file = os.path.join(self.t, "config")
        with open(cfg_file, "w") as f:
            f.write(f"MEETING_COPILOT_PORT={port}\nMEETING_COPILOT_OPEN_CMD={opener} --new-tab {{url}} --bg\n")
        launcher = os.path.join(ROOT, "bin", "meeting-copilot")
        env = dict(self.env)
        env.pop("MEETING_COPILOT_PORT", None)
        env.update(MEETING_COPILOT_DB=db, MEETING_COPILOT_CONFIG=cfg_file, OPENED=opened,
                   MEETING_COPILOT_CLAUDE=os.path.join(self.t, "no-claude"), MEETING_COPILOT_PYTHON=PY)
        try:
            p = subprocess.run(["bash", launcher], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn(f"http://127.0.0.1:{port}", p.stdout)        # the port came from the file
            with open(opened) as f:
                self.assertRegex(f.read(), rf"^--new-tab\|http://127\.0\.0\.1:{port}/\?once=[\w-]{{20,}}\|--bg\|$")
            # without {url}, the URL goes at the end (a later line wins)
            with open(cfg_file, "a") as f:
                f.write(f"MEETING_COPILOT_OPEN_CMD={opener} -x\n")
            p = subprocess.run(["bash", launcher], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            with open(opened) as f:
                self.assertRegex(f.read(), rf"^-x\|http://127\.0\.0\.1:{port}/\?once=[\w-]{{20,}}\|$")
        finally:
            subprocess.run(["bash", launcher, "stop"], env=env, capture_output=True, text=True, timeout=30)

    @unittest.skipIf(WIN, NO_BASH_WHY)
    def test_open_cmd_that_keeps_running_does_not_hold_the_launcher(self):
        db = os.path.join(self.t, "muesli.db")
        fake_meeting.prepare(copilot.Path(db))
        port = free_port()
        opened = os.path.join(self.t, "opened.txt")
        opener = os.path.join(self.t, "my-browser")
        with open(opener, "w") as f:   # like a browser binary started directly: it stays running
            f.write('#!/bin/sh\nprintf "%s|" "$@" > "$OPENED"\nexec sleep 60\n')
        os.chmod(opener, 0o755)
        cfg_file = os.path.join(self.t, "config")
        with open(cfg_file, "w") as f:
            f.write(f"MEETING_COPILOT_PORT={port}\nMEETING_COPILOT_OPEN_CMD={opener} {{url}} --again {{url}}\n")
        launcher = os.path.join(ROOT, "bin", "meeting-copilot")
        env = dict(self.env)
        env.update(MEETING_COPILOT_DB=db, MEETING_COPILOT_CONFIG=cfg_file, OPENED=opened,
                   MEETING_COPILOT_CLAUDE=os.path.join(self.t, "no-claude"), MEETING_COPILOT_PYTHON=PY)
        try:
            p = subprocess.run(["bash", launcher], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               stdin=subprocess.DEVNULL, text=True, timeout=20)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            wait_for(lambda: os.path.exists(opened) and os.path.getsize(opened) > 0, timeout=5)
            with open(opened) as f:
                got = f.read()
            url = got.split("|")[0]
            self.assertRegex(url, rf"^http://127\.0\.0\.1:{port}/\?once=[\w-]{{20,}}$")
            self.assertEqual(got, f"{url}|--again|{url}|")      # every {url} is the URL
        finally:
            subprocess.run(["bash", launcher, "stop"], env=env, capture_output=True, text=True, timeout=30)
            subprocess.run(["pkill", "-f", opener], capture_output=True)

    @unittest.skipIf(WIN, NO_BASH_WHY)
    def test_open_cmd_that_fails_is_reported(self):
        db = os.path.join(self.t, "muesli.db")
        fake_meeting.prepare(copilot.Path(db))
        port = free_port()
        cfg_file = os.path.join(self.t, "config")
        with open(cfg_file, "w") as f:
            f.write(f"MEETING_COPILOT_PORT={port}\nMEETING_COPILOT_OPEN_CMD=false\n")
        launcher = os.path.join(ROOT, "bin", "meeting-copilot")
        env = dict(self.env, MEETING_COPILOT_DB=db, MEETING_COPILOT_CONFIG=cfg_file,
                   MEETING_COPILOT_CLAUDE=os.path.join(self.t, "no-claude"), MEETING_COPILOT_PYTHON=PY)
        try:
            p = subprocess.run(["bash", launcher], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("MEETING_COPILOT_OPEN_CMD failed", p.stderr)
            self.assertIn(f"open http://127.0.0.1:{port}/ in your browser", p.stdout)
            self.assertNotIn(self.token(), p.stdout + p.stderr)
        finally:
            subprocess.run(["bash", launcher, "stop"], env=env, capture_output=True, text=True, timeout=30)


if __name__ == "__main__":
    unittest.main()
