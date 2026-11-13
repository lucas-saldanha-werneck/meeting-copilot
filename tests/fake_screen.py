#!/usr/bin/env python3
"""Stand-ins for the macOS screen commands used by the tests. They never capture anything.

  fake_screen.py capture <screencapture args...>   copies $FAKE_SC_IMAGE to the last argument (exit 1 and
                                                   no file when $FAKE_SC_FAIL is set)
  fake_screen.py windows                           prints $FAKE_SC_WINDOWS (a JSON file) as the window list
Each call appends one JSON line (argv) to $FAKE_SC_LOG.
"""
import json
import os
import shutil
import sys

argv = sys.argv[1:]
with open(os.environ["FAKE_SC_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(argv) + "\n")
if argv[0] == "windows":
    with open(os.environ["FAKE_SC_WINDOWS"], encoding="utf-8") as f:
        sys.stdout.write(f.read())
    sys.exit(0)
if os.environ.get("FAKE_SC_FAIL"):
    sys.stderr.write("could not create image from window\n")
    sys.exit(1)
shutil.copyfile(os.environ["FAKE_SC_IMAGE"], argv[-1])
