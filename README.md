# meeting-copilot

A live meeting copilot for macOS. While a meeting is recording in the [Muesli](https://github.com/Muesli-HQ/muesli) app, it reads Muesli's live transcript (read-only) and shows a local web page with:

- **Say this / Ask this**: up to two short sentences or questions you could use right now.
- **Corrections**: something said that looks wrong, contradicts an earlier statement, has an inconsistent number, or is a commitment with no owner or deadline.
- **Goals**: you type the meeting's goals; they get checked off as the conversation covers them.
- **Relevant facts** (optional): looked up in folders of notes you choose and/or MCP tools you allow.
- **Warnings** (no AI): long monologue, talking most of the last 5 minutes, few questions, speaking too fast.

The AI runs through `claude -p` (Claude Code), so it uses your Claude Code login. One AI call runs at a time: suggestions about every 25 s, fact search about every 45 s.

## Status: read this first

- **It depends on the Muesli app.** It reads Muesli's SQLite database (`~/Library/Application Support/Muesli/muesli.db`), table `meeting_transcript_checkpoints`, which Muesli fills during a recording. This is Muesli's internal format, not a public API, and a Muesli update can change it. It was built against the format of Muesli 0.8.5.
- **It was tested only with a simulated meeting, not a real one.** The simulation writes invented lines into a separate SQLite file with the same tables (`tools/fake_meeting.py`). One run used the real `claude -p` with a notes folder; the automated tests use a fake `claude`. The MCP option was tested only with the fake `claude`.
- **macOS only** (because Muesli is). The tests also run on Linux and Windows, where the parts that need a shell script or POSIX signals are skipped with a printed reason.

## Example

```
$ meeting-copilot
meeting-copilot on http://127.0.0.1:4390 - start a meeting in Muesli
```

Output from a simulated meeting (invented conversation about renewing a cleaning contract, with a notes file that says the increase is 12%):

```
Say this      Fifteen percent on four thousand is six hundred, so about forty-six hundred a month.
Ask this      Who will take this to the board, and by what date?
Corrections   "someone will sort it out" - Commitment with no owner and no deadline.
              -> Let's name an owner and a date for the board review before we close.
Facts         Internal, don't say out loud: your notes show the proposed increase is 12%, not 15%.
              Source: contracts.md
Warnings      You spoke 71% of the last 5 min. Listen more.
```

## Install

Requirements: macOS, Python 3.9+, [Muesli](https://github.com/Muesli-HQ/muesli), and [Claude Code](https://code.claude.com) (`claude` on your PATH, logged in).

```
git clone https://github.com/lucas-saldanha-werneck/meeting-copilot
cd meeting-copilot
./install.sh            # symlinks bin/meeting-copilot into ~/.local/bin (--dry-run to preview)
```

## Use

```
meeting-copilot             start (if needed) and open the page in your default browser
meeting-copilot --no-open   start and only print the URL
meeting-copilot status
meeting-copilot log
meeting-copilot stop        stop it (the session is saved)
```

Start a recording in Muesli; the page switches to "live" within a few seconds. Open "Objective and goals" on the page to type what you want out of the meeting.

To try it without a real meeting:

```
python3 tools/fake_meeting.py prepare /tmp/test.db
meeting-copilot --db /tmp/test.db
python3 tools/fake_meeting.py speak /tmp/test.db 4     # one invented line every 4 s
python3 tools/fake_meeting.py end /tmp/test.db
```

## Your settings

Your settings live in your own file, outside the program folder, so updating meeting-copilot
(`git pull` or a new download) never touches them:

- `~/.config/meeting-copilot/config` (or `$XDG_CONFIG_HOME/meeting-copilot/config` when
  `XDG_CONFIG_HOME` is set), or any file named by `MEETING_COPILOT_CONFIG`
- one `KEY=VALUE` per line; `#` starts a comment; `~` and `$VARS` expand in paths
- each key also works as an environment variable, which wins over the file

You don't have to create it: the first `meeting-copilot` command you run creates it, a copy of
[`config.example`](config.example) with every key listed at its default and commented out (so it
changes nothing until you remove a `#` and edit the line). If the file is already there, it is
never rewritten or changed, not even by an update.

Every key:

| Variable | Default | Meaning |
|---|---|---|
| `MEETING_COPILOT_DB` | `~/Library/Application Support/Muesli/muesli.db` | Muesli database (opened read-only) |
| `MEETING_COPILOT_PORT` | `4390` | Local port; the server listens on 127.0.0.1 only |
| `MEETING_COPILOT_CLAUDE` | `claude` on PATH | Path to the Claude Code CLI |
| `MEETING_COPILOT_MODEL` | `sonnet` | Model for suggestions (`MEETING_COPILOT_DATA_MODEL` for fact search) |
| `MEETING_COPILOT_SEARCH_DIRS` | empty | Folders the AI may search for facts, separated by `:` |
| `MEETING_COPILOT_MCP_CONFIG` | empty | An MCP config file (`{"mcpServers": {...}}`) for fact search |
| `MEETING_COPILOT_MCP_TOOLS` | empty | Comma-separated allowlist of tools from that file, e.g. `mcp__notes__search`. Required: without it MCP stays off |
| `MEETING_COPILOT_SESSIONS_DIR` | `~/.local/share/meeting-copilot/sessions` | Where each meeting's transcript and suggestions are saved when it ends (`MEETING_COPILOT_SAVE_SESSIONS=0` to turn off) |
| `MEETING_COPILOT_SAVE_FORMAT` | `json` | `markdown`: save a readable `<Muesli export name>-copilot.md` (suggestions, corrections, facts, warnings; no transcript) instead of the JSON. It waits up to `MEETING_COPILOT_MUESLI_WAIT_S` seconds (600) for Muesli's auto-export, reads its name from Muesli's export log and links to it. The file says it is AI-written: keep it out of folders the fact search reads, or it can come back as a "fact" |
| `MEETING_COPILOT_EXTRA_HOSTS` | empty | Extra host names allowed to load the page, e.g. behind your own reverse proxy or tunnel (POSTs then accept `https://<host>` as origin) |
| `MEETING_COPILOT_CONFIRM_AI` | `0` | `1`: no AI call in a meeting until you click "Turn on AI" on the page; each meeting asks again. Useful when the copilot starts at login and should not spend AI on every recording |
| `MEETING_COPILOT_ASSIST_S`, `_DATA_S`, `_POLL_S` | `25`, `45`, `5` | Intervals in seconds |
| `MEETING_COPILOT_CONTEXT_FILE` | empty | A text file about you that both AI calls read: your name and role, your projects, what your notes folders and MCP tools hold, words the transcript tends to get wrong. The first 8000 characters are used |
| `MEETING_COPILOT_LANGUAGE` | empty (the meeting's language) | Language for every suggestion and fact, e.g. `Brazilian Portuguese` |
| `MEETING_COPILOT_PROMPT_FILE` | empty (the built-in prompt) | A text file that replaces the built-in suggestions prompt. See [Your own prompt](#your-own-prompt) |
| `MEETING_COPILOT_DATA_PROMPT_FILE` | empty (the built-in prompt) | A text file that replaces the built-in fact-search prompt. See [Your own prompt](#your-own-prompt) |
| `MEETING_COPILOT_OPEN_CMD` | `open` (macOS) or `xdg-open` | Command that opens the page. The URL is passed as one argument wherever `{url}` is (no quotes around it), or at the end, e.g. `open -g -a "Firefox"` |
| `MEETING_COPILOT_DATA_DIR`, `_LOG`, `_PYTHON` | `~/.local/share/meeting-copilot`, its `meeting-copilot.log`, `python3` | Data folder, log file and Python used by `meeting-copilot` (environment only for `_LOG` and `_PYTHON`) |

Example config:

```
MEETING_COPILOT_SEARCH_DIRS=$HOME/notes:$HOME/work/docs
MEETING_COPILOT_MCP_CONFIG=$HOME/.config/meeting-copilot/mcp.json
MEETING_COPILOT_MCP_TOOLS=mcp__chat__MessageHistory,mcp__chat__ContactFindOne
MEETING_COPILOT_CONTEXT_FILE=~/.config/meeting-copilot/context.md
MEETING_COPILOT_LANGUAGE=Brazilian Portuguese
MEETING_COPILOT_OPEN_CMD=open -g -a "Google Chrome"
```

Example `context.md`:

```
I am Ana, head of operations at a small solar installer.
My notes: ~/notes is my wiki (one page per client). The chat tools read WhatsApp, read-only:
session "ana" is personal, session "shop" is the company number.
The transcript often writes "Solarix" as "solar X".
```

Only allow read-only MCP tools: the fact search runs during your meeting without asking you.

### Your own prompt

Two settings each name a text file that replaces one built-in prompt (start from a copy of it):

- `MEETING_COPILOT_PROMPT_FILE`: the suggestions prompt (`PROMPT_ASSIST` in `lib/copilot.py`, the
  call behind "Say this", "Ask this", corrections and goals).
- `MEETING_COPILOT_DATA_PROMPT_FILE`: the fact-search prompt (`PROMPT_DATA`, the "Relevant facts"
  call).

Placeholders (written as is, with the braces):

| Placeholder | Prompt | Becomes |
|---|---|---|
| `{language}` | both | The language name from `MEETING_COPILOT_LANGUAGE` (e.g. `answer in {language}`), or `the language the meeting is using` when that is empty |
| `{context}` | both | Your `MEETING_COPILOT_CONTEXT_FILE` text under a heading, or nothing when that is empty |
| `{sources}` | fact search | The folders (`MEETING_COPILOT_SEARCH_DIRS`) and tools (`MEETING_COPILOT_MCP_TOOLS`) it may search, one line each |

The built-in prompts get the language line ("Write every item in ...") and the context block added
at their end. Your file gets the same for any part it does not place; a fact-search file that does
not place `{sources}` gets "You can:" and that list at the end. Placeholders are filled in one pass,
so a `{word}` inside your context text is never filled. Other `{words}` stay as written, and
`{sources}` in the suggestions file is not a placeholder. If a file is cut at the 20000-character
limit, the cut never splits a placeholder.

What must stay:

- Suggestions: the reply always comes back as JSON with the fields `say`, `ask`, `corrections` and
  `goals_covered` (Claude Code's `--json-schema`), so your text cannot break the parser. But if it
  never asks for `say` and `ask`, those lists may stay empty.
- Fact search: the reply is free text. A reply that starts with `EMPTY` means "nothing found" and
  shows nothing; anything else shows as a fact. Keep telling the AI to answer exactly `EMPTY` when
  it has nothing, or every "I found nothing" shows up on the page. When your fact-search file places
  `{language}` and a language is set, a line asking to keep `EMPTY` in English is added at the end.

At startup the log shows a `note:` line when a suggestions file never mentions `say` or `ask`, when
a fact-search file never contains the word `EMPTY` (in capitals), or when a file uses a `{word}` that
is not one of its placeholders.

If a file is missing, unreadable, not UTF-8 or empty, the log shows one `note:` line (a separate
one for a file that is not UTF-8) and that call uses its built-in prompt. Only the first 20000
characters of a file are used (the prompt goes on the `claude` command line).

## Optional requirements

| Optional | What it adds | Without it |
|---|---|---|
| Claude Code (`claude`) | Suggestions, corrections, goals, facts | Only the speaking metrics and warnings work; the page says `claude` was not found |
| Search folders | Facts from your notes (the AI uses Read, Grep and Glob inside them only) | No fact search from files |
| MCP config + tool allowlist | Facts from other sources (chat history, a CRM, ...) | No fact search from MCP |

With neither search folders nor MCP, the "Relevant facts" panel says fact search is off and only the suggestion call runs.

## Privacy

- The Muesli database is opened read-only and never written.
- The transcript of the last 3 minutes, your goals and the items already shown are sent to Claude through `claude -p` on each call. Nothing else leaves the machine.
- Sessions (full transcript plus suggestions) are saved as JSON files readable only by your user.
- The page answers only on 127.0.0.1 / localhost (plus any `MEETING_COPILOT_EXTRA_HOSTS`), checks the `Host` header against DNS rebinding, and accepts POSTs only from its own origin.

## Uninstall

```
meeting-copilot stop
./install.sh --uninstall
```

Your config and saved sessions are kept; delete `~/.config/meeting-copilot` and `~/.local/share/meeting-copilot` to remove them.

## Test

```
python3 tests/test_copilot.py -v
```

The tests build a fake Muesli database with an invented meeting, put a fake `claude` first on PATH, and run the real program as a process. They never touch the real Muesli database or call any AI.

## License

MIT

Built with Claude Code
