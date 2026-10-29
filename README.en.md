# codex-inbox · AI task inbox

中文说明：[README.md](README.md)

When you run many Codex and Claude Code sessions at once, it is easy to lose track of which one is waiting for your reply, which one is waiting on a colleague, and which one has a release scheduled tonight. codex-inbox is a local dashboard on your Mac. It reads the transcripts both tools already keep on disk, groups sessions by who they are waiting on, notifies you when something is due, and keeps its own copy of what you asked, the links the AI gave you, and the images you sent.

The name only says Codex to keep it short. **Both Codex (desktop app and CLI) and Claude Code are supported.**

**The UI and notifications are in Chinese**, and so are the six status keywords the AI writes (see "Status line" below).

## Let your AI install it

Send this to your Claude Code or Codex. It follows [INSTALL.md](INSTALL.md) to install and set everything up, and only stops when something needs you in person (signing in through the browser, clicking Allow on a system prompt, agreeing to a security setting change):

```text
Install codex-inbox for me: https://github.com/SeraFxy/codex-inbox — follow INSTALL.md in the repo step by step until it is done.
```

INSTALL.md is written in Chinese; coding agents follow it fine. To install by hand, see "Install" below.

## What it does

- **Groups sessions by who they are waiting on**: four tabs on the left: your turn (needs you, waiting for your sign-off) / AI working / waiting (for a time, on a colleague) / to tidy up. Click a row to see details on the right. Keys: J / K to move, O to open the session, E to mark done, S to snooze, ⌘K to search. Drag rows to reorder them. The grouping comes from a status line the AI writes at the end of each reply. Sessions without one are still listed, marked as "status unknown".
- **Spots stuck sessions**: an AI waiting on a question or an approval prompt, a message of yours that has had no reply for 30 minutes, or a turn that stopped halfway and went quiet are all moved to "needs you".
- **Reminders**: if the status line carries a future time, the session waits under "waiting for a time"; when it is due it moves to "needs you" and a Mac notification pops up. Clicking the notification opens the dashboard. Every card can be snoozed for 15 minutes, 1 hour, 3 hours, until tonight, until tomorrow morning or until next Monday.
- **Decisions waiting on you**: when the AI lists what it needs you to decide as `❓ question｜option｜option` lines, the expanded card shows each one with option buttons and builds a single reply from your picks (unpicked ones are written as 「待定」, "undecided", and you can add a note). The 「复制并打开会话」 (copy and open) button copies the reply and opens the session; you paste it with ⌘V and press Enter yourself. The dashboard never sends for you: every message makes the AI re-read the whole conversation, so you decide when to send.
- **"What I said"**: every message you sent to Codex or Claude, grouped by session, with multi-keyword search and per-message copy. Claude Code deletes transcripts after 30 days by default; this keeps a plain-text copy (about 20 MB a year).
- **Keeps goals, links and images**: Codex goals, the 🎯 goal lines, links the AI gave you in its replies (Feishu docs, merge requests, GitHub and so on; links inside code blocks and command lines are skipped), and images you sent to the AI (deduplicated by content). They stay even if the original session is deleted.
- **"Done" is not lost**: a session you mark done moves to the bottom and comes back by itself when you talk to it again.
- **Closing a cmux tab marks the session done**: no need to mark it done in both places; the dashboard moves it to "Done" on its next refresh. Sessions that are scheduled, waiting on a colleague, or waiting for a time that has not come yet are left alone (you probably closed them to free memory, and they still need to remind you). Reopen the session in cmux and it comes back.
- **Session names and tags**: a session's name comes from the name you gave it in the cmux terminal sidebar, if any. Project tags can be renamed and given an emoji (they stay gray: color is reserved for status).
- **Optional features** (they need extra tools; without them only these features are unavailable, everything else works):
  - Scheduled wake-ups: a Claude session running in the cmux terminal can use `ledger schedule` to resume itself at a set time (for example, a release tonight). Needs cmux, with cmux set to allow background programs to control it (`ledger cmux-setup`, see "Known limitations").
  - Daily Feishu digest: with the Feishu CLI `lark-cli` installed and `feishu_open_id` set in the config, a summary is sent to you as a Feishu direct message every morning. `ledger feishu-setup` fills it in and sends a test.

Everything is read from local files. **It makes no AI calls and spends no tokens.** The server only listens on `127.0.0.1`; the only network request is the optional Feishu digest.

## Install

Requires macOS and the system `/usr/bin/python3` (3.9 or later). No dependencies.

```sh
git clone https://github.com/SeraFxy/codex-inbox.git ~/codex-inbox
~/codex-inbox/install.sh
```

Then open <http://127.0.0.1:47800>. Pinning it as a browser tab is recommended. Afterwards run `~/codex-inbox/ledger doctor`: it checks the installation item by item (read-only) and tells you the next step for anything missing.

- **Do not install it under Desktop, Documents or Downloads**: macOS does not let background services read those folders. `install.sh` refuses and tells you so.
- Installing registers a launchd service that starts at login (default label `local.codex-inbox`, change it with the `CODEX_INBOX_LABEL` environment variable) and compiles a small notifier app named 「AI 任务台账」. It shows a welcome notification when installation finishes; click Allow when macOS asks whether to allow its notifications.
- After updating the code, run `install.sh` again to restart the service. `uninstall.sh` stops and removes the service; your data stays in `data/`.

## Status line

The dashboard groups sessions by the last line of each AI reply. The rules live in [status-line.md](status-line.md) (in Chinese). After installing, run once:

```sh
~/codex-inbox/ledger rules install
```

It appends the rules to `~/.codex/AGENTS.md` (Codex) and `~/.claude/CLAUDE.md` (Claude Code), wrapped in `<!-- codex-inbox:status-line:start -->` / `<!-- codex-inbox:status-line:end -->` markers, with the full local path of `ledger schedule` filled in for 已定时. It only touches the tools you have installed, and skips a file that already has a hand-written status-line rule (one containing `📌 <状态>`). Run it again after updating to refresh the block in place; `ledger rules show` previews it and `ledger rules remove` deletes only what is between the markers. Only new sessions pick up the rules.

If you prefer English wording, add the version below by hand instead. Keep the six status words, `目标：` and `完成标准：` in Chinese exactly as written: the dashboard matches on them. The one-line summary after the status can be in any language.

```markdown
## Task inbox status line
End every reply addressed directly to me with a status line (skip it when running as a subagent or non-interactively):
`📌 <status>｜<one-line summary>｜<time, optional>`
Use exactly one of these six status words, in Chinese:
- 等你 (waiting on me): write the concrete action I need to take next, not "please review".
- 等同事 (waiting on a colleague): write it as 「等同事（name）」; the time is when to follow up.
- 等时间点 (waiting for a time): something I must do myself at a set time; the time is required.
- 已定时 (scheduled): you have scheduled yourself to resume at a set time; the time is required (currently only Claude sessions in cmux support this).
- AI在做 (AI working): you are still running in the background and do not need me yet.
- 待你确认完成 (done, awaiting my sign-off): the completion criteria are met; give the evidence in one sentence. Never write "done" yourself.
Write times as `YYYY-MM-DD HH:MM` in the same time zone as the dashboard. Do not use 📌 or 🎯 anywhere else in the reply.
In the first reply of a task, and whenever the goal or the completion criteria change, add this line above the 📌 line:
`🎯 目标：<one-line goal>｜完成标准：<what counts as done>`
When you need me to decide things, put one line per decision above the 📌 line (not inside a code block):
`❓ <what I need to decide>｜<option 1>｜<option 2>`
Keep each option short enough to stand as my answer, at most 6; leave them out when there are no fixed choices. These lines are the list itself; don't repeat it in the body. I will answer one per line as `1. <decision>：<my pick>`, with 「待定」 for anything I haven't decided.
```

For Claude Code sessions to use 已定时, also tell them to schedule the wake-up with this command (replace the path with your install directory):

```sh
~/codex-inbox/ledger schedule --at "YYYY-MM-DD HH:MM" --note "<one line shown on the dashboard>" --prompt "<what to do when it is time>"
```

## Configuration

The first install copies `config.example.json` to `config.json`. Run `install.sh` after editing it. All keys are optional:

| Key | Default | Meaning |
|---|---|---|
| `port` | `47800` | Dashboard port |
| `scan_interval_min` | `15` | How often the service reads transcripts in the background; while the dashboard page is open it also reads once a minute |
| `window_days` | `14` | Only sessions active within this many days are shown |
| `stale_hours` | `48` | Sessions quiet for longer than this go to "to tidy up" |
| `interrupted_min` | `30` | How long a turn can sit stopped halfway before it counts as possibly stuck |
| `digest_time` | `"09:00"` | Time of the Feishu digest |
| `feishu_open_id` | empty | Your Feishu open_id; leave empty to send no digest |
| `feishu_as` | `"bot"` | Which `lark-cli` identity sends the digest |
| `wake_expire_hours` | `3` | A scheduled wake-up that is late by more than this is dropped (for example, the Mac was asleep) |
| `utc_offset_hours` | empty | Time zone the dashboard works in; empty means the Mac's own time zone. Set it when your Mac's time zone differs from the one you schedule work in, e.g. `8` if you are abroad but work on Beijing time. Times in status lines and `ledger schedule` are read in this zone too |
| `project_parents` | `[]` | Every subfolder of these folders counts as a project, e.g. `["~/code", "~/work/repos"]`. When empty, projects are guessed from `~/Desktop/<project>` and `~/<project>` |

## Commands

```sh
./install.sh                 # install or restart the service
./uninstall.sh               # stop and remove the service
./ledger scan -v             # scan once by hand and print the groups
./ledger schedules           # recent scheduled wake-ups
./ledger cancel <id>         # cancel a scheduled wake-up
./ledger digest              # preview the Feishu digest (--send to send it)
./ledger doctor              # check the installation item by item and print next steps (read-only)
./ledger rules install       # add the status-line rules (remove to delete them, show to preview)
./ledger feishu-setup        # set up the Feishu digest and send a test (--dry-run to only look)
./ledger cmux-setup          # let the dashboard service control cmux, needed for wake-ups (--dry-run to only look)
```

## What it reads and stores

| Source | Used for |
|---|---|
| `~/.claude/projects/*/*.jsonl`, `~/.claude/history.jsonl` | Claude Code sessions, your messages and images |
| `~/.claude/sessions/*.json` | Whether an open Claude session is running, idle, or showing an approval prompt |
| `~/.codex/sessions/`, `~/.codex/*.sqlite`, `~/.codex/session_index.jsonl` | Codex sessions, titles, goals, your messages and images |
| `~/Library/Application Support/cmux/session-*.json` | Session names you set in the cmux sidebar (read only) |

Stored in the install directory (none of it goes into git; `data/` and `logs/` are readable only by your own account): `data/ledger.db` (session status, your actions, your messages, goals, links), `data/images/` (images, a few hundred take one or two hundred MB), `logs/`, `config.json`.

## Tested versions

Used day to day on the setup below. Other versions will probably work but have not been tested:

- macOS 26.5 with the system Python 3.9
- Codex desktop app (ChatGPT.app 26.1023.51421, bundled codex-cli 0.165.0-alpha.1.2)
- Claude Code 2.1.312

The Codex side reads Codex's internal files, whose names carry a version number: `~/.codex/state_5.sqlite` (session source and title), `goals_1.sqlite` (goals), `thread_history_1.sqlite` (your messages and links in AI replies), plus `session_index.jsonl` and the transcripts under `sessions/`. If a Codex update renames these files or changes their structure, the affected features (titles, goals, the Codex part of "What I said") may stop finding data and the log will say so. Grouping relies on the transcripts under `sessions/` and is normally unaffected.

## Known limitations

- Only sessions active within the last 14 days are shown. Subagents, review agents and `claude -p` runs are left out. Threads with a Codex automation (heartbeat) attached are threads you started, so they are shown as usual; for ones you don't want to see, such as daily reminders, click 「不再跟踪」 (stop tracking) once.
- "Open session": Codex sessions open through `codex://threads/<id>`. Claude sessions jump to their cmux window, but by default cmux only accepts control from programs started in cmux terminals, so the login service is refused; the dashboard then shows a hint and does not start a new session for you. Scheduled wake-ups have the same limitation. To allow it, switch cmux to password mode: add `"automation": {"socketControlMode": "password", "socketPassword": "<a long random password>"}` to `~/.config/cmux/cmux.json` and run `cmux reload-config` (or change "Socket Control Mode" and set a password under cmux Settings → Automation). The cmux CLI reads this password on its own, so the dashboard needs no extra configuration; programs running in cmux terminals are not affected. `ledger cmux-setup` can make this change for you (if the file has comments, it only prints instructions instead of editing it).
- While the Mac sleeps the service sleeps too; notifications and wake-ups catch up after it wakes. Wake-ups late by more than `wake_expire_hours` are skipped.
- The dashboard's read endpoints have no password. Web pages cannot read them (the server checks the Host and Origin headers), but any program on this Mac can read the session list, your messages and images. Changes (marking done, snoozing, editing tags and so on) require a token. Programs running under your own account could read the database in `data/` directly anyway, so a password would not stop them; if other people have accounts on this Mac, keep this in mind.

## License

MIT, see [LICENSE](LICENSE).

## Files

| File | Purpose |
|---|---|
| `ledger.py` (`ledger` is a shortcut to it) | All the logic, Python standard library only |
| `board.html` | The dashboard page |
| `notifier/` | Source of the notifier app (`notify.applescript`), its build script and the icon (drawn to the Blink design spec; source in `icon/icon.svg`) |
| `install.sh` / `uninstall.sh` | Install / remove the login service |
| `status-line.md` | The single source of the status-line rules; `ledger rules install` reads it |
| `INSTALL.md` | Install steps written for AI coding agents (in Chinese) |
