#!/usr/bin/env python3
"""The text search and ranking of the fast fact search, in one file with only Python's standard library, so the
same code runs in two places:
  - imported by copilot.py, for the search on this machine;
  - sent over ssh as the program of `python3 -I - '<json>'` on the remote machine (FACT_SEARCH=remote): it runs
    ripgrep there, keeps the lines with a whole word, scores them and prints only the best few as JSON, so the
    Mac gets a few KB instead of every line found (up to 6 MB for one question, 27/11). The file is read from
    this machine at each call, so the server never runs an older copy.

Scores from both places can be put together: the remote answer carries the raw counts (how many lines found
each word, and how many lines in all) and, for each line it sends, the words in the line and in its file
name; the Mac adds its own counts and scores every line again with the same formula (rank()).
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata

LINE_CHARS = 300          # of one line found (a .jsonl line can be a whole conversation)
PER_FILE = 200            # lines taken from one file
FILE_MAX_BYTES = 20_000_000
PATH_WEIGHT = 1.0         # a word in the file or folder name counts this much of a word in the line
POOL = 2                  # the remote machine sends n * POOL lines, so the Mac's own counts can still reorder

FOLD_CLASS = {"a": "[aáàâãä]", "e": "[eéèêë]", "i": "[iíìîï]", "o": "[oóòôõö]", "u": "[uúùûü]", "c": "[cç]",
              "n": "[nñ]"}


def fold(text: str) -> str:
    """Lower case, without accents."""
    return "".join(ch for ch in unicodedata.normalize("NFD", text.lower()) if not unicodedata.combining(ch))


def term_pattern(term: str, rg: bool = False) -> str:
    """A regex that finds the term as a whole word (a plural s/es may follow), with or without accents (any
    case, with the ignore-case flag), for Python's re on NFC text (see nfc()). rg=True: the pre-filter for
    ripgrep, which reads the files as they are: the term anywhere (Unicode word boundaries make ripgrep 7-9
    times slower with several patterns), each letter also followed by combining marks (an NFD "ç" is
    c + U+0327). Only the lines the Python pattern finds are kept (describe())."""
    if rg:
        return "".join(FOLD_CLASS[ch] + r"\p{M}*" if ch in FOLD_CLASS else re.escape(ch) for ch in term)
    body = "".join(FOLD_CLASS.get(ch, re.escape(ch)) for ch in term)
    return rf"\b{body}(?:e?s)?\b"


def nfc(text: str) -> str:
    return text if text.isascii() else unicodedata.normalize("NFC", text)


def split_lines(text: str) -> list[str]:
    """Lines as ripgrep numbers them: split at \\n only (a \\f or U+2028 inside a line stays in it)."""
    return [ln[:-1] if ln.endswith("\r") else ln for ln in text.split("\n")]


def rg_args(pats: list[str], skip: list[str], context: int = 0) -> list[str]:
    # --no-ignore: every file a .gitignore/.ignore would hide is searched too, as the Python search does
    # (hidden files and folders stay out in both)
    args = ["-i", "-n", "--no-heading", "--with-filename", "--null", "--no-messages", "--color", "never",
            "--no-ignore",
            "--max-columns", str(LINE_CHARS), "--max-columns-preview", "--max-count", str(PER_FILE),
            "--max-filesize", str(FILE_MAX_BYTES)]
    if context:
        args += ["--context", str(context)]
    for g in skip:
        args += ["-g", "!" + g]
    for p in pats:
        args += ["-e", p]
    return args


def rg_out(returncode: int, stdout: bytes) -> list[tuple[str, int, str]]:
    """The lines ripgrep found: (path, number, text); each printed as path NUL number : text (a --context
    line has - after the number and is skipped)."""
    # 1: no line found; 2: an error, often one unreadable file among lines found (those are kept)
    if returncode not in (0, 1) and not (returncode == 2 and stdout.strip()):
        raise RuntimeError(f"rg exited {returncode}")
    hits = []
    for raw in stdout.decode("utf-8", "replace").split("\n"):
        path, _, rest = raw.partition("\0")
        m = re.match(r"(\d+)([:-])", rest)
        if not m:
            continue                         # the "--" between context groups, an empty last line
        num, text = int(m.group(1)), rest[m.end():].rstrip("\r")
        if m.group(2) == ":":
            hits.append((path, num, nfc(text)[:LINE_CHARS]))
    return hits


def describe(hits: list[tuple[str, int, str]], terms: list[str]) -> tuple[list[dict], list[int]]:
    """(candidates, counts): each line found with ids = the words (by index) it has as whole words and
    pids = the words its file name has (its folder/file); counts[i] = how many lines found have word i. A line
    with no whole word (the ripgrep pre-filter also finds "taxi" for "tax") is not a candidate and is not
    counted.
    One scan of each line for all the words (5 words over 10 800 lines: 0.12 s with one scan per word); every
    match is one whole run of word characters, so checking each word matched against every pattern gives the
    same words as one search per pattern."""
    pats = [term_pattern(t) for t in terms]
    rxs = [re.compile(p, re.IGNORECASE) for p in pats]
    anyword = re.compile("|".join(f"(?:{p})" for p in pats), re.IGNORECASE)
    which: dict[str, list[int]] = {}
    counts = [0] * len(rxs)
    names: dict[str, list[int]] = {}
    out = []
    for path, num, text in hits:
        found: set[int] = set()
        for m in anyword.finditer(text):
            w = m.group()
            if w not in which:
                which[w] = [i for i, r in enumerate(rxs) if r.fullmatch(w)]
            found.update(which[w])
        if not found:
            continue
        ids = sorted(found)
        for i in ids:
            counts[i] += 1
        if path not in names:
            name = nfc(os.path.basename(os.path.dirname(path)) + "/" + os.path.basename(path))
            names[path] = [i for i, r in enumerate(rxs) if r.search(name)]
        out.append({"path": path, "line": num, "text": text, "ids": ids, "pids": names[path]})
    return out, counts


def idf(counts: list[int], total: int, weights: list[float] | None = None) -> list[float]:
    """How rare each word is among the lines ripgrep found (total: every one, also those without a whole
    word, as before the ranking moved here), times its weight (Jev: how much the word
    names what is asked; 1 without Jev)."""
    w = weights if weights and len(weights) == len(counts) else [1.0] * len(counts)
    return [w[i] * math.log(1 + total / (1 + c)) for i, c in enumerate(counts)]


def rank(cands: list[dict], counts: list[int], total: int, weights: list[float] | None, n: int,
         roots: list[str]) -> list[dict]:
    """The n best candidates, best first, each with its score: the rarity of each word in the line, plus
    PATH_WEIGHT times that for a word only in its file's path (a WhatsApp export named after the company);
    near-identical lines count once; on a tie, the earlier search folder (roots) first."""
    f = idf(counts, total, weights)
    scored = []
    for c in cands:
        ids = c["ids"]
        score = sum(f[i] for i in ids) + PATH_WEIGHT * sum(f[i] for i in c["pids"] if i not in ids)
        if not ids or score <= 0:
            continue
        r = next((k for k, root in enumerate(roots) if c["path"].startswith(root)), len(roots))
        scored.append((-score, r, c["path"], c["line"], c))
    scored.sort(key=lambda x: x[:4])
    out, keys = [], set()
    for neg, _, _, _, c in scored:
        key = fold(" ".join(c["text"].split()))[:120]
        if key in keys:
            continue
        keys.add(key)
        out.append({**c, "score": round(-neg, 3)})
        if len(out) >= n:
            break
    return out


def contexts(path: str, nums: list[int]) -> dict[int, str]:
    """{num: line num (from 1) and the one above and below it, without blank ones} for one file, read only up
    to the last line needed and split at \\n as ripgrep numbers lines (a .jsonl file of several MB with the
    lines found near its top is not read to its end: 0.09 s for 14 files saved, 27/11)."""
    want = {k for n in nums for k in (n - 1, n, n + 1)}
    last, got = max(want, default=0), {}
    try:
        with open(path, "rb") as f:
            for k, raw in enumerate(f, 1):
                if k in want:
                    got[k] = raw.rstrip(b"\n").rstrip(b"\r").decode("utf-8", "replace")
                if k >= last:
                    break
    except OSError:
        pass
    return {n: "\n".join(nfc(got[k].strip())[:LINE_CHARS] for k in (n - 1, n, n + 1) if got.get(k, "").strip())
            for n in nums}


def add_contexts(cands: list[dict]) -> None:
    """Each candidate without a context gets one (each file read once)."""
    by: dict[str, list[dict]] = {}
    for c in cands:
        if "context" not in c:
            by.setdefault(c["path"], []).append(c)
    for path, cs in by.items():
        ctx = contexts(path, [c["line"] for c in cs])
        for c in cs:
            c["context"] = ctx[c["line"]]


def remote_search(req: dict) -> dict:
    """What the remote machine does for one round: ripgrep over req["dirs"], the whole-word check, the counts,
    and the best n * POOL lines, each with the line above and below read from its file (ripgrep's --context
    for every line found doubled its output and its time). req: terms, dirs, skip, n, weights, timeout
    (seconds ripgrep may take), rg (its command, default rg on the PATH)."""
    t0 = time.monotonic()
    terms, dirs = req["terms"], req["dirs"]
    rg = req.get("rg") or shutil.which("rg") or "rg"
    pats = [term_pattern(t, rg=True) for t in terms]
    p = subprocess.run([rg, *rg_args(pats, req.get("skip") or []), "--", *dirs], capture_output=True,
                       stdin=subprocess.DEVNULL, timeout=float(req.get("timeout") or 10))
    rg_s = time.monotonic() - t0
    hits = rg_out(p.returncode, p.stdout)
    cands, counts = describe(hits, terms)
    best = rank(cands, counts, len(hits), req.get("weights"), int(req.get("n") or 10) * POOL, dirs)
    add_contexts(best)
    for c in best:
        c.pop("score")
    return {"counts": counts, "total": len(hits), "hits": best, "rg_s": round(rg_s, 3),
            "s": round(time.monotonic() - t0, 3)}


def main(argv: list[str]) -> int:
    try:
        out = remote_search(json.loads(argv[1]))
    except (OSError, ValueError, KeyError, IndexError, RuntimeError, subprocess.SubprocessError) as ex:
        sys.stderr.write(f"factrank: {type(ex).__name__}: {ex}\n")
        return 3
    sys.stdout.write(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
