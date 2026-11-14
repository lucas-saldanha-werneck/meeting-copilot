#!/usr/bin/env python3
"""A stand-in for `claude -p` used by the tests. It never calls any AI.

One-shot mode (`--output-format json`): appends one JSON line per call to $FAKE_CLAUDE_LOG (argv, stdin,
cwd) and prints a reply in that shape: structured_output when --json-schema is given (the suggestions
call), a plain result otherwise (the fact-search call).

Kept-open mode (`--input-format stream-json`, the suggestions process): logs {"spawn": true, argv, pid}
when it starts, then reads one JSON user message per line and, for each, logs {argv, stdin: <the
message>, cwd, pid, turn} and prints stream-json events ending with a result line whose text is the
suggestions JSON. It ends at the end of its input. Test knobs (environment):
  FAKE_CLAUDE_SLEEP       seconds to wait before each answer
  FAKE_CLAUDE_HANG_ONCE   a path: the first process to find it missing creates it and never answers
  FAKE_CLAUDE_BAD_ONCE    a path: the first answer to find it missing creates it and replies with prose
  FAKE_CLAUDE_TOKENS      context tokens reported per turn (grows with each turn of the process)
  FAKE_CLAUDE_REFUSE_KEY  1: a process started with ANTHROPIC_API_KEY answers "Credit balance is too low"
Every entry has "key": whether ANTHROPIC_API_KEY was set (never its value).
A message with images (content blocks) is logged with "text" and "images" ([{media_type, len}]) in both modes.
One-shot with images (`--input-format stream-json` plus --json-schema): the stream-json events ending with the
same result object as `--output-format json`.
"""
import json
import os
import sys
import time

argv = sys.argv[1:]
LOG = os.environ["FAKE_CLAUDE_LOG"]
SUGGESTIONS = {
    "say": ["Let me confirm the fifteen percent before we decide."],
    "ask": ["Who on the board will own the window cleaning question, and by when?"],
    "corrections": [{
        "quote": "someone will sort it out",
        "problem": "Commitment with no owner and no deadline.",
        "suggestion": "Can we name who follows up and set a date?",
    }],
    "goals_covered": [0],
}


def log(entry):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def first_time(path):
    """True once per path, across processes (the file is created atomically)."""
    if not path:
        return False
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def stream_mode():
    key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    log({"spawn": True, "argv": argv, "cwd": os.getcwd(), "pid": os.getpid(), "key": key})
    hang = first_time(os.environ.get("FAKE_CLAUDE_HANG_ONCE"))
    per_turn = int(os.environ.get("FAKE_CLAUDE_TOKENS", "0") or 0)
    turn = 0
    for raw in sys.stdin:
        if not raw.strip():
            continue
        msg = json.loads(raw)["message"]["content"]
        turn += 1
        entry = {"argv": argv, "stdin": msg, "cwd": os.getcwd(), "pid": os.getpid(), "turn": turn, "key": key}
        if isinstance(msg, list):
            entry["stdin"] = msg[0]["text"]
            entry["text"] = "\n".join(b["text"] for b in msg if b["type"] == "text")
            entry["images"] = [{"media_type": b["source"]["media_type"], "len": len(b["source"]["data"])}
                               for b in msg if b["type"] == "image"]
        log(entry)
        emit({"type": "system", "subtype": "init"})
        if key and os.environ.get("FAKE_CLAUDE_REFUSE_KEY") == "1":
            emit({"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "modelUsage": {},
                  "result": "Credit balance is too low"})
            continue
        if hang:
            time.sleep(3600)          # a stuck call: silent, never answers
        time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))
        emit({"type": "stream_event", "event": {"type": "message_start"}})
        text = "Sorry, I cannot help with that." if first_time(os.environ.get("FAKE_CLAUDE_BAD_ONCE")) \
            else "```json\n" + json.dumps(SUGGESTIONS) + "\n```"
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
        emit({"type": "result", "subtype": "success", "is_error": False, "result": text, "num_turns": 1,
              "usage": {"input_tokens": 4, "cache_read_input_tokens": per_turn * turn,
                        "cache_creation_input_tokens": 0, "output_tokens": 50}})


stream = "--input-format" in argv and argv[argv.index("--input-format") + 1] == "stream-json"
if stream and "--json-schema" not in argv:      # the kept-open process; a one-shot call has a schema
    stream_mode()
    sys.exit(0)

stdin = sys.stdin.read()
stream = "--input-format" in argv and argv[argv.index("--input-format") + 1] == "stream-json"
entry = {"argv": argv, "stdin": stdin, "cwd": os.getcwd()}
if stream:
    msg = json.loads(stdin.splitlines()[0])["message"]
    entry["text"] = "\n".join(b["text"] for b in msg["content"] if b["type"] == "text")
    entry["images"] = [{"media_type": b["source"]["media_type"], "len": len(b["source"]["data"])}
                       for b in msg["content"] if b["type"] == "image"]
    entry["stdin"] = msg["content"][0]["text"]
log(entry)
if os.environ.get("FAKE_CLAUDE_STDERR"):
    sys.stderr.write(os.environ["FAKE_CLAUDE_STDERR"] + "\n")
    sys.stderr.flush()
time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))   # a slow or stuck call

if "--json-schema" in argv:
    res = {"type": "result", "subtype": "success", "result": "", "structured_output": SUGGESTIONS,
           "total_cost_usd": 0.0}
    if stream:
        print(json.dumps({"type": "system", "subtype": "init"}))
        print(json.dumps({"type": "assistant", "message": {"content": []}}))
    print(json.dumps(res))
else:
    print(json.dumps({"type": "result", "subtype": "success",
                      "result": "The current contract ends on 2027-03-31.\nSource: contracts.md"}))
