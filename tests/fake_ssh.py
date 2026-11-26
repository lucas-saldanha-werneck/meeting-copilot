#!/usr/bin/env python3
"""A stand-in for `ssh` used by the tests: no network. It skips ssh's options, takes the host, joins the rest
of the command line with spaces and runs it with /bin/sh -c, as sshd runs a remote command through the
user's shell (so an argument that was not quoted for that shell breaks here as it would there).
It runs the command with FAKE_CLAUDE_WHERE=remote, so the fake claude logs where it ran. Test knobs:
  FAKE_SSH_LOG    a file: one JSON line per call with ssh's argv
  FAKE_SSH_DOWN   a path: while it exists, "Connection refused" and exit 255 (the machine is out)
  FAKE_SSH_HANG   a path: while it exists, a connection that never answers (no output, no exit)
"""
import json
import os
import sys
import time

WITH_VALUE = set("BbcDEeFIiJLlmOoPpQRSWw")
argv = sys.argv[1:]
if os.environ.get("FAKE_SSH_LOG"):
    with open(os.environ["FAKE_SSH_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": argv}) + "\n")
i = 0
while i < len(argv) and argv[i].startswith("-"):
    i += 2 if len(argv[i]) == 2 and argv[i][1] in WITH_VALUE else 1
host, command = argv[i], " ".join(argv[i + 1:])
down, hang = os.environ.get("FAKE_SSH_DOWN"), os.environ.get("FAKE_SSH_HANG")
if down and os.path.exists(down):
    sys.stderr.write(f"ssh: connect to host {host} port 22: Connection refused\n")
    sys.exit(255)
if hang and os.path.exists(hang):
    time.sleep(3600)
os.environ["FAKE_CLAUDE_WHERE"] = "remote"
os.execv("/bin/sh", ["/bin/sh", "-c", command])
