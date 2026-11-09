#!/usr/bin/env python3
"""A stand-in for `claude -p` used by the tests. It never calls any AI.

It appends one JSON line per call to $FAKE_CLAUDE_LOG (argv, stdin, cwd) and prints a reply in
the shape of `claude -p --output-format json`: structured_output when --json-schema is given
(the suggestions call), a plain result otherwise (the fact-search call).
"""
import json
import os
import sys
import time

argv = sys.argv[1:]
stdin = sys.stdin.read()
with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": argv, "stdin": stdin, "cwd": os.getcwd()}) + "\n")
if os.environ.get("FAKE_CLAUDE_STDERR"):
    sys.stderr.write(os.environ["FAKE_CLAUDE_STDERR"] + "\n")
    sys.stderr.flush()
time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))   # a slow or stuck call

if "--json-schema" in argv:
    out = {
        "say": ["Let me confirm the fifteen percent before we decide."],
        "ask": ["Who on the board will own the window cleaning question, and by when?"],
        "corrections": [{
            "quote": "someone will sort it out",
            "problem": "Commitment with no owner and no deadline.",
            "suggestion": "Can we name who follows up and set a date?",
        }],
        "goals_covered": [0],
    }
    print(json.dumps({"type": "result", "subtype": "success", "result": "", "structured_output": out}))
else:
    print(json.dumps({"type": "result", "subtype": "success",
                      "result": "The current contract ends on 2027-03-31.\nSource: contracts.md"}))
