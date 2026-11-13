# meeting-copilot

A live meeting copilot for macOS. While a meeting is recording in the [Muesli](https://github.com/Muesli-HQ/muesli) app, it reads Muesli's live transcript (read-only) and shows a local web page with:

- **Say this / Ask this**: up to two short sentences or questions you could use right now.
- **Corrections**: something said that looks wrong, contradicts an earlier statement, has an inconsistent number, or is a commitment with no owner or deadline.
- **Goals**: you type the meeting's goals; they get checked off as the conversation covers them.
- **Relevant facts** (optional): looked up in folders of notes you choose and/or MCP tools you allow.
- **Warnings** (no AI): long monologue, talking most of the last 5 minutes, few questions, speaking too fast.

The AI runs through `claude -p` (Claude Code), so it uses your Claude Code login. Suggestions about every 25 s, fact search about every 45 s.

### The kept-open AI process (suggestions)

Starting `claude -p` costs several seconds (4-12 s on a busy Mac) while the answer itself takes 1-3 s. So the
suggestions use one `claude` process per meeting that stays open (`--input-format stream-json`):

- **When it starts:** as soon as the AI is allowed, before the first round: the "Turn on AI" click (for the
  live meeting or for the next one), the start of a meeting when the AI is allowed (always, without
  `CONFIRM_AI`), and a restart of the copilot that gives back the click. Starting it costs no AI call.
- **When it closes:** AI turned off, the meeting ends, a "next meeting" click that expires (only with `NEXT_MEETING_ARM_MIN`), and the
  copilot's exit. If the copilot is killed, the process ends by itself at the end of its input.
- **Each round** sends only what is new since the previous round (new transcript lines, changed goals). The
  first message, and the first after any restart of the process, has the goals, the items already shown and
  the last 3 minutes of the transcript, as a one-shot call has.
- **Watchdog:** no first byte from the API after 6 s, or no answer after 10 s (4 s more for a process still
  starting), and the process is killed, a fresh one is seeded and the round is tried once more. A second
  failure drops that round (the next one runs on a fresh process). So a stuck call costs at most about 20 s
  instead of the old 2-minute timeout.
- **Bounded context:** after 40,000 tokens of context or 150 rounds the process is replaced by a fresh,
  seeded one, so a long meeting does not grow without limit.
- **Reply format:** plain JSON in the text (one model turn); `--json-schema` costs a second turn. An answer
  that is not JSON is dropped and logged; the round still counts.
- The fact search keeps one `claude -p` per call (it uses tools), with a 40 s limit, and runs beside the
  suggestions so they never wait behind it. With `MEETING_COPILOT_PERSISTENT_AI=0` every suggestions round is
  one `claude -p` call (30 s limit) and one AI call runs at a time.
- **API credit:** with the claude-credito wrapper installed (`MEETING_COPILOT_API_CREDIT`), the kept-open
  process follows its policy itself (the wrapper keeps a call's whole output until it ends, so it cannot carry
  a kept-open process): the plan's monthly API credit first, with the wrapper's stored key (macOS Keychain
  or its key file) put only into the process's environment; while that key is paused, the subscription.
  When the API refuses the key ("Credit balance is too low", "Invalid API key" and the like), the copilot
  writes the wrapper's own pause file for that key (so the wrapper and other tools also move to the
  subscription), adds a line to the wrapper's log, restarts the process on the subscription and sends the
  same round again. The wrapper's settings (`CLAUDE_CREDITO*`, its config file) are honored.
- `/state` has `ai_rounds`: the last rounds' timings in seconds from the request (`sent`, `first_byte`,
  `result`), with `turn`, `seeded`, `attempts` and `tokens`. `tools/bench_live.py` measures rounds.

## Status: read this first

- **It depends on the Muesli app.** It reads Muesli's SQLite database (`~/Library/Application Support/Muesli/muesli.db`), table `meeting_transcript_checkpoints`, which Muesli fills during a recording. This is Muesli's internal format, not a public API, and a Muesli update can change it. It was built against the format of Muesli 0.8.5.
- **It was tested only with a simulated meeting, not a real one.** The simulation writes invented lines into a separate SQLite file with the same tables (`tools/fake_meeting.py`). One run used the real `claude -p` with a notes folder; the automated tests use a fake `claude`. The MCP option was tested only with the fake `claude`.
- **macOS only** (because Muesli is). The tests also run on Linux and Windows, where the parts that need a shell script or POSIX signals are skipped with a printed reason.

## Example

```
$ meeting-copilot
meeting-copilot on http://127.0.0.1:4390/ - start a meeting in Muesli
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
meeting-copilot status      say whether it runs, with every page URL (local, LAN, extra hosts) and the pairing code
meeting-copilot code        show a pairing code to log in another browser or device
meeting-copilot devices     list the browsers logged in with a code (`devices forget` logs them all out)
meeting-copilot log
meeting-copilot stop        stop it (the session is saved)
```

### Logging in

The page is private. `meeting-copilot` logs the Mac's own browser in by itself when it opens the page. Any other browser (another browser on the Mac, your phone, a tablet) shows a login page the first time:

1. Open the page on that device (for example `http://copilot.local`, or your tunnel's address).
2. It asks: "Type the 6-digit code shown on the Mac".
3. The code shows on the Mac as a notification. It is also in `meeting-copilot status` and `meeting-copilot code`, and behind the "Pair a device" button on a page that is already logged in.
4. Type it. That browser stays logged in for a year.

A code is valid for 5 minutes and works once. There is one code at a time: every login page opened in those 5 minutes gets the same code, and the Mac shows the notification once (the login page has a button to show it again). After 5 wrong tries the code is thrown away; after 10 wrong tries in a minute, from anywhere, every try waits until the minute is over. After 20 wrong tries since the last right one, the login locks: no new code is made for a browser until you run `meeting-copilot code` on the Mac (or press "Pair a device" on a logged-in page), which unlocks it and shows a fresh code. Time alone never unlocks it, so a device on your network that keeps guessing gets at most 20 tries in a million each time you unlock. Each logged-in browser gets its own cookie (`HttpOnly`, `SameSite=Strict`, `Secure` behind https), kept on the Mac only as a hash in `devices.json` in your data folder. `meeting-copilot devices` lists them and `meeting-copilot devices forget` logs them all out at once.

The URLs that `meeting-copilot` and `meeting-copilot status` print have no password in them. Under the code there is still a random token, made on first start in your data folder (`~/.local/share/meeting-copilot/token`, or under `$XDG_DATA_HOME` or `MEETING_COPILOT_DATA_DIR` when set; readable only by you). It stays the same across restarts; `python3 lib/copilot.py --token` prints it, and deleting the file makes a new one at the next start. The launcher uses it to open the Mac's browser with a one-time link (valid 60 seconds, once). An old `…/?token=…` link still works, and a script can send the token as `Authorization: Bearer <token>`:

```
curl -s -H "Authorization: Bearer $(cat ~/.local/share/meeting-copilot/token)" http://127.0.0.1:4390/state
```

### From your phone or another computer

Two optional ways, both off by default:

- **Home network** (`MEETING_COPILOT_LAN=1`): a second listener on `0.0.0.0` port 80, published as `http://copilot.local` with macOS `dns-sd` (the name follows the Mac's address when it changes). It answers only private addresses (`10/8`, `172.16/12`, `192.168/16`, `169.254/16`, `fc00::/7`, `fe80::/10` and loopback); anything else gets `403` before the login is even looked at. Open the `LAN:` URL that `meeting-copilot status` prints on each device and type the code once. Devices that do not resolve `.local` names can use the `LAN IP:` URL. This is plain http: anyone on the same Wi-Fi who can watch its traffic can see the code and the cookie, so use it only on a network you trust.
- **Internet, through a tunnel** (`MEETING_COPILOT_EXTRA_HOSTS=<your host>`): point a tunnel (e.g. Cloudflare Tunnel, with Cloudflare Access in front) at `http://127.0.0.1:4390` and add its host name. The page then accepts that `Host` and the `https://<host>` origin; when the tunnel says the visit came over https (`X-Forwarded-Proto` or `Cf-Visitor`), the cookies are also marked `Secure`. `meeting-copilot status` prints it as `public: https://<host>/`; the first visit from each device asks for the code.

For example, in your settings file:

```
MEETING_COPILOT_LAN=1
MEETING_COPILOT_LAN_NAME=copilot
MEETING_COPILOT_EXTRA_HOSTS=copilot.example.com
```

On macOS your user may listen on `0.0.0.0:80` without root (not on Linux: pick a `MEETING_COPILOT_LAN_PORT` above 1023 there). macOS may ask once to allow incoming connections for Python. If the port is taken, the log says so and only the LAN page stays off.

Start a recording in Muesli; the page switches to "live" within a few seconds. Open "Objective and goals" on the page to type what you want out of the meeting.

To try it without a real meeting:

```
python3 tools/fake_meeting.py prepare /tmp/test.db
meeting-copilot --db /tmp/test.db
python3 tools/fake_meeting.py speak /tmp/test.db 4     # one invented line every 4 s
python3 tools/fake_meeting.py end /tmp/test.db
```

`prepare` refuses a file that already exists; add `--force` to replace it.

## Settings

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
| `MEETING_COPILOT_PORT` | `4390` | Local port; the server listens on 127.0.0.1 (plus the optional LAN listener) |
| `MEETING_COPILOT_CLAUDE` | `claude` on PATH | Path to the Claude Code CLI |
| `MEETING_COPILOT_API_CREDIT` | `claude-credito` | Optional wrapper for the AI calls (`on` = this default name) ([api-credit](https://github.com/lucas-saldanha-werneck/api-credit)). When this command is installed (on PATH or in `~/.local/bin`), calls go through it: your plan's monthly API credit first, and the same call again on the subscription when the API refuses. Not installed: nothing changes. `off` never uses it |
| `MEETING_COPILOT_MODEL` | `sonnet` | Model for suggestions (`MEETING_COPILOT_DATA_MODEL` for fact search) |
| `MEETING_COPILOT_SEARCH_DIRS` | empty | Folders the AI may search for facts, separated by `:` |
| `MEETING_COPILOT_MCP_CONFIG` | empty | An MCP config file (`{"mcpServers": {...}}`) for fact search |
| `MEETING_COPILOT_MCP_TOOLS` | empty | Comma-separated allowlist of tools from that file, e.g. `mcp__notes__search`. Required: without it MCP stays off |
| `MEETING_COPILOT_SESSIONS_DIR` | `~/.local/share/meeting-copilot/sessions` | Where each meeting's transcript and suggestions are saved when it ends (`MEETING_COPILOT_SAVE_SESSIONS=0` to turn off) |
| `MEETING_COPILOT_SAVE_FORMAT` | `json` | `markdown`: save a readable `<Muesli export name>-copilot.md` (suggestions, corrections, facts, warnings; no transcript) instead of the JSON. It waits up to `MEETING_COPILOT_MUESLI_WAIT_S` seconds (600) for Muesli's auto-export, reads its name from Muesli's export log and links to it. The file says it is AI-written: keep it out of folders the fact search reads, or it can come back as a "fact" |
| `MEETING_COPILOT_EXTRA_HOSTS` | empty | Extra host names allowed to load the page, e.g. behind your own reverse proxy or tunnel (POSTs then accept `https://<host>` as origin; the cookie gets `Secure` when the tunnel reports https) |
| `MEETING_COPILOT_LAN` | `0` | `1`: also answer devices on your home network, on `0.0.0.0:<LAN_PORT>`, private addresses only. See [From your phone or another computer](#from-your-phone-or-another-computer) |
| `MEETING_COPILOT_LAN_PORT` | `80` | Port of the LAN listener (80: no port in the URL) |
| `MEETING_COPILOT_LAN_NAME` | `copilot` | Published as `<name>.local` (macOS `dns-sd`); `off`: not published |
| `MEETING_COPILOT_LAN_IP` | empty (found by itself, checked every 30 s) | A fixed LAN address to publish instead |
| `MEETING_COPILOT_DNS_SD` | `dns-sd` on macOS | The `dns-sd` command; elsewhere no name is published |
| `MEETING_COPILOT_OSASCRIPT` | `osascript` on macOS | The command that shows the pairing code as a notification; `off`: no notification (the code is still in `meeting-copilot status`) |
| `MEETING_COPILOT_CONFIRM_AI` | `0` | `1`: no AI call in a meeting until you click "Turn on AI" on the page; each meeting asks again. Useful when the copilot starts at login and should not spend AI on every recording. The click, the objective and the goals are kept in `context.json` in your data folder (only your user can read it), so a restart during a meeting gives them back; a file older than 6 hours is ignored |
| `MEETING_COPILOT_NEXT_MEETING_ARM_MIN` | `0` | With `CONFIRM_AI=1`: minutes a "Turn on AI for the next meeting" click waits for a meeting to start. `0`: no limit, it waits until the next meeting (turn it off with the same button). Once the meeting starts, the click counts for the whole meeting |
| `MEETING_COPILOT_ASSIST_S`, `_DATA_S`, `_POLL_S` | `25`, `45`, `5` | Intervals in seconds |
| `MEETING_COPILOT_PERSISTENT_AI` | `1` | `1`: one kept-open `claude` process per meeting for the suggestions (see "The kept-open AI process"). `0`: a new `claude -p` per round |
| `MEETING_COPILOT_AI_FIRST_BYTE_S`, `_AI_ROUND_S` | `6`, `10` | Watchdog of a suggestions round on the kept-open process: seconds to the API's first byte, and to the answer |
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
- The transcript of the last 3 minutes, your goals and the items already shown are sent to Claude through `claude -p` (on the kept-open process: once, then only the new lines each round). Nothing else leaves the machine.
- Sessions (full transcript plus suggestions) are saved as JSON files readable only by your user. A meeting saved again (the copilot restarted, or the meeting stopped and recorded again) gets a new file with `-2`, `-3`, and so on, so nothing earlier is overwritten.
- The fact search may read files only inside the search folders: Read, Grep and Glob are allowed with a path rule for each folder, so a request in the meeting to open any other file is refused. The AI runs in an empty folder of its own and loads no Claude Code settings files, so no hook planted in a search folder runs with it.
- The page answers only on 127.0.0.1 / localhost (plus any `MEETING_COPILOT_EXTRA_HOSTS`, and, with `MEETING_COPILOT_LAN=1`, private addresses on the home network, where POSTs must come from the page's own `http://<host>` origin), checks the `Host` header against DNS rebinding, and accepts POSTs only from its own origin. Every request (the page, `/state` and each POST) needs a login: the cookie of a browser that typed a pairing code, the token cookie (set by the launcher's one-time link or a `?token=` URL), or the token as an `Authorization: Bearer` header; anything else gets `401` (a browser gets the login page). The code is never shown to a browser that is not logged in, not even on the Mac itself: only the notification, `meeting-copilot status`/`code` and a logged-in page show it. The login POST passes the same `Host` and origin checks as every POST, and the code is compared in constant time. The token file and `devices.json` are readable only by your user, so other users' programs and web pages cannot read the meeting; a program running as you can still read the files.

## Uninstall

```
meeting-copilot stop
./install.sh --uninstall
```

Your config and saved sessions are kept; delete `~/.config/meeting-copilot` and `~/.local/share/meeting-copilot` to remove them.

## Tests

```
python3 tests/test_copilot.py -v
```

The tests build a fake Muesli database with an invented meeting, put a fake `claude` first on PATH, and run the real program as a process. They never touch the real Muesli database or call any AI.

## License

MIT

Built with Claude Code
