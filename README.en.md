# codex-inbox · AI task inbox

中文说明：[README.md](README.md)

When you run many Codex and Claude Code sessions at once, it is easy to lose track of which one is waiting for your reply, which one is waiting on a colleague, and which one has a release scheduled tonight. codex-inbox is a local dashboard on your Mac. It reads the transcripts both tools already keep on disk, groups sessions by who they are waiting on, notifies you when something is due, and keeps its own copy of what you asked, the links and files the AI gave you, and the images you sent.

The name only says Codex to keep it short. **Both Codex (desktop app and CLI) and Claude Code are supported.**

**The UI and notifications are in Chinese**, and so are the six status keywords the AI writes (see "Status line" below).

## Let your AI install it

Send this to your Claude Code or Codex. It follows [INSTALL.md](INSTALL.md) to install and set everything up, and only stops when something needs you in person (signing in through the browser, clicking Allow on a system prompt, agreeing to a security setting change):

```text
Install codex-inbox for me: https://github.com/SeraFxy/codex-inbox — follow INSTALL.md in the repo step by step until it is done.
```

INSTALL.md is written in Chinese; coding agents follow it fine. To install by hand, see "Install" below.

## What it does

- **Groups sessions by who they are waiting on**: four tabs: your turn (needs you, waiting for your sign-off) / AI working / waiting (for a time, on a colleague) / to tidy up. Click a row to see details on the right. Keys: J / K to move, O to open the session, E to mark done, S to snooze, P to pin, [ to hide or show the left column, ⌘K to search. Pinned sessions stay on top; the rest are sorted by last update, newest first. The grouping comes from a status line the AI writes at the end of each reply. Sessions without one are still listed, marked as "status unknown".
- **Filter by project and topic**: the leftmost column lists your projects, and each project can have topics (for example 「客户A」 or 「评测」 under one project). Click one and the queue shows only its sessions; a red number means something in it is your turn. There are three ways to file a session under a topic: start or end a message to Claude or Codex with `#topic` followed by a space (or write `#topic#` anywhere); the last one you wrote wins, and a topic that does not exist yet is created under that project. A `#word` in the middle of a sentence or inside code only matches an existing topic and never creates one; drag a row onto a topic in the left column; or use the row's ⋯ menu → 「归到主题…」 (file under topic). Filing by hand wins: later `#topic` tags no longer move that session. Topics can be renamed (old `#name` tags still match) and deleted. A session belongs to the project of its folder by default; if that is wrong (say a general daily automation was created inside one project's folder), use the row's ⋯ menu → 「移到项目…」 (move to project) or drag the row onto a project in the left column to move it to another project, or type a new name (such as 「定时任务」) to create one; you can move it back any time. If the project name comes from the wrong folder level, click a segment of the path under 「位置」 (Location) in the detail pane's session info: from then on, sessions started anywhere in that folder belong to a project named after it. You can undo it, or click 「恢复默认」 (Reset) to go back to the default. Double-click a project or topic to rename it; drag projects to reorder them; a project's or topic's ⋯ menu can archive it: archived ones leave 「全部」 (All) and are listed under 「归档」 (Archive) at the bottom of the left column, and come back on their own when one of their sessions gets new activity. Star a session (row ⋯ menu or the star in the detail header) to keep it under 「收藏」 (Favorites) in the left column, even after it is done; click it to jump to it or reopen it. Rename a session by double-clicking its title in the detail pane (or row ⋯ → 「改名…」): the name changes in Claude / Codex and on the cmux tab too, by appending one rename record; no AI call. The header switch shows only CC or only Codex sessions. Sessions that a Codex automation runs in get a gray 「定时」 (scheduled) tag; hover it to see which automation and when it runs (paused ones are lighter). These sessions are also listed automatically under 「定时任务」 (Scheduled) in the left column, next to Favorites and regardless of project, with when each one runs; paused ones fold into a 「已暂停的 N 个」 (N paused) row.
- **Spots stuck sessions**: an AI waiting on a question or an approval prompt, a message of yours that has had no reply for 30 minutes, or a turn that stopped halfway and went quiet are all moved to "needs you".
- **Reminders**: if the status line carries a future time, the session waits under "waiting for a time"; when it is due it moves to "needs you" and a Mac notification pops up. Clicking the notification opens the dashboard. Every card can be snoozed for 15 minutes, 1 hour, 3 hours, until tonight, until tomorrow morning, for 3 days (until 9 am) or until next Monday.
- **Decisions waiting on you**: when the AI lists what it needs you to decide as `❓ question｜option｜option` lines, the card counts as waiting on you (even if it says the work is done), and the expanded card shows each one with option buttons and builds a single reply from your picks (unpicked ones are written as 「待定」, "undecided"); you can edit the reply right in the box. When the AI adds a `荐：` part naming the option it recommends and why, that option gets a 「荐」 mark and is picked by default, with the reason shown under the question; if you changed your picks and want the recommendations back, click 「全按推荐」 (take the recommendations). The dashboard also remembers questions asked in earlier replies that you haven't answered: the card shows 「N 件没答」 (N unanswered), and the details list them under 「之前问过、还没答」 so you can still pick an answer, or dismiss them with 「不用答了」. A question counts as answered once your reply has a `1. <question>：<pick>` line for it or mentions its ID (such as Q-031). Copying the question without its ID also works, with a colon, bar, space, underscore or dash between the question and your answer; if the AI asks it again, it is listed again. The 「复制并打开会话」 (copy and open) button copies the reply and opens the session; you paste it with ⌘V and press Enter yourself. The dashboard never sends for you: every message makes the AI re-read the whole conversation, so you decide when to send. When the AI writes 「等你」 (waiting on you) without any decision to make, it needs you to do something: the card reads 「要你做：…」 (for you to do), and the details show it in its own 「要你做」 section, where 「做完了，你接着做」 (done, carry on) drafts the reply that tells it to continue.
- **Calls it made for you**: when the AI decides something without asking you and notes it as a `🧭 what it decided｜why` line, the card shows 「替你定了 N 件」 (made N calls for you) and the details list the latest 20 under 「它替你定的」 (decided for you), newest first. If you disagree, 「改」 (change) copies 「关于你替我定的「…」，我想改成：」 ("about the call you made …, I'd like it changed to:") and opens the session, so you finish the sentence there.
- **Context usage**: works out how full each session's context is from the token usage recorded with each reply; no AI involved. At 70% the row shows 「上下文 N%」 (context N%) and the details add a one-line hint to finish the current task and continue in a new session; the session info always shows the percentage. Claude sessions are measured against the `autoCompactWindow` set for the model in `~/.claude/settings.json` (the size at which it compacts), or 200,000 if none is set; Codex sessions use the context window recorded in their logs, so the number matches what Codex shows.
- **Compaction handoff**: when a Claude session runs out of context it compacts, and while doing so it writes itself a handoff (the goal, what is done, what is left). For a long session that says far more than the first and last messages. The detail pane shows the latest one under the conversation, collapsed to the first lines, verbatim and without calling any AI. The status-line rules ask for the handoff in Chinese; for older English ones, 「翻译成中文」 (translate to Chinese) uses the browser's built-in on-device translation (Chrome 138 and later), so it costs no tokens, and the result is kept in that browser. Codex stores its compaction summaries encrypted, so only Claude sessions have this.
- **"What I said"**: every message you sent to Codex or Claude, grouped by session, with multi-keyword search and per-message copy. Claude Code deletes transcripts after 30 days by default; this keeps a plain-text copy (about 20 MB a year).
- **One search box for both**: ⌘K or / opens "All", which lists the AI's outputs (top 3) together with what you said, also searching Claude's compaction handoffs and what the AI wrote for you: the questions it asked (❓, answered ones too), the calls it made for you, its status line and the session goal, each labeled where it matched, so you don't have to know whether you said it or the AI gave it. "What I said" and "Outputs" narrow it to one side; all three tabs share the same search terms, kept when you switch, and show their hit counts. Click 「前后对话」 (context) next to any hit to open the conversation around it right in the results: what you said and the AI's final reply for each turn, with the search terms highlighted, and you can keep reading earlier or later. If the transcript has been deleted, you get the line the dashboard saved and what you said around it. "‹ Board" at the top left, or Esc, takes you back to the board.
- **Outputs**: links the AI gave you in its replies (Feishu docs, merge requests, GitHub and so on) and local files (Markdown, web pages, PDFs, spreadsheets, images and the like, written as a path or a `[name](path)` link; the file has to exist). Anything inside code blocks and command lines is skipped. Deliverables (docs, files, web pages) stay; merge requests, pipelines and commits are temporary, so they sit in their own "MRs & pipelines" group and are not counted as deliverables. "Outputs" in the header filters them by Feishu / files / web / code and searches titles, links, the sentence the AI wrote and session names; each link is listed once. Every entry shows what the AI said when it gave it and which sessions gave it; click a session name to jump back to its details on the board (sessions no longer on the board open directly). Local files open in their default app; if the git worktree a file lived in has been cleaned up, the same file in the main working tree opens instead (or, when the main working tree is not on the default branch, the default-branch version on GitLab / GitHub). Nothing is copied, so it takes no extra disk space. A session's details list the latest 8 deliverables, with "show all" and a filter; below them come the session's MRs and pipelines, with only the still-open MRs spread out and the merged ones folded. With `lark-cli` installed and signed in as you, Feishu docs show their real titles (one Feishu API call per 200 docs, no AI; set `feishu_doc_titles` to `false` to turn it off). With `glab` installed and signed in to your GitLab, MRs show their real titles and open / merged / closed, and "Open MRs" lists only the unmerged ones (one call per 40 MRs per project, open ones rechecked every 15 minutes, no AI; set `gitlab_mr_state` to `false` to turn it off).
- **Keeps goals and images**: Codex goals, the 🎯 goal lines, and images you sent to the AI (deduplicated by content). They stay even if the original session is deleted. Sessions found in "What I said" have a "show on board" button that takes you back to their status, goal and outputs.
- **"Done" is not lost**: a session you mark done moves to the bottom and comes back by itself when you talk to it again.
- **Closing a cmux tab marks the session done**: no need to mark it done in both places; the dashboard moves it to "Done" on its next refresh. Sessions that are scheduled, waiting on a colleague, or waiting for a time that has not come yet are left alone (you probably closed them to free memory, and they still need to remind you). Reopen the session in cmux and it comes back. It also works the other way: clicking 「完成」 (Done) or 「不再跟踪」 (Stop tracking) on the dashboard closes the session's cmux tab, so the tabs left open are the ones you still need to follow. A session whose AI has not stopped (still running, or waiting for your approval) is left open; if its workspace holds other tabs (a browser, say), only its own tab is closed; a group's title tab is never closed. If you undo Done and want to keep going, 「打开会话」 (Open session) reopens it in cmux.
- **Session names and tags**: a session's name comes from the name you gave it in the cmux terminal sidebar, if any. Project tags can be renamed and given an emoji (they stay gray: color is reserved for status).
- **Optional features** (they need extra tools; without them only these features are unavailable, everything else works):
  - Scheduled wake-ups: a Claude session running in the cmux terminal can use `ledger schedule` to resume itself at a set time (for example, a release tonight). Needs cmux, with cmux set to allow background programs to control it (`ledger cmux-setup`, see "Known limitations").
  - Daily Feishu digest: with the Feishu CLI `lark-cli` installed and `feishu_open_id` set in the config, a summary is sent to you as a Feishu direct message every morning. `ledger feishu-setup` fills it in and sends a test.

Everything is read from local files. **It makes no AI calls and spends no tokens.** The server only listens on `127.0.0.1`; the only network requests are the optional Feishu digest and Feishu doc titles.

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
- 等你 (waiting on me): write the concrete action I need to take next, not "please review". Leave the time out (the dashboard always treats 等你 as my turn now); if I should do it at a later time, use 等时间点.
- 等同事 (waiting on a colleague): write it as 「等同事（name）」; the time is when to follow up.
- 等时间点 (waiting for a time): something I must do myself at a set time; the time is required.
- 已定时 (scheduled): you have scheduled yourself to resume at a set time; the time is required (currently only Claude sessions in cmux support this).
- AI在做 (AI working): you are still running in the background and do not need me yet. Also use it while waiting for a pipeline, build or deploy to finish, and actually watch it: in Claude Code, poll its status with a background command (you get woken up when it exits); in Codex, wait for it within the turn; for long waits, schedule yourself with `ledger schedule` and write 已定时. Until it finishes, do not write 等你 or 待你确认完成: I cannot do anything yet.
- 待你确认完成 (done, awaiting my sign-off): the completion criteria are met; give the evidence in one sentence, using the finished result (e.g. "pipeline #91313 passed"), not "triggered". Never write "done" yourself.
Write times as `YYYY-MM-DD HH:MM` in the same time zone as the dashboard. Do not use 📌, 🎯 or 🧭 anywhere else in the reply.
In the first reply of a task, and whenever the goal or the completion criteria change, add this line above the 📌 line:
`🎯 目标：<one-line goal>｜完成标准：<what counts as done>`
- Write the completion criteria as results that can be checked when you finish (e.g. "the page shows X", "all tests pass", "deployed to production and the endpoint responds"), not "work on it for 4 hours" or "optimize as much as you can".
- Don't loosen the completion criteria or switch goals along the way. If you can't meet them, stop and say so; ask with a ❓ line before changing them.
- If you are truly stuck (three different approaches to the same problem have failed), stop going in circles: write 等你 and say where you are stuck, what you tried and what you need from me.
When to stop and ask me:
- Don't ask about things that can be undone; just do them and tell me afterwards: changing code or docs, committing on your own branch, opening an MR, running tests, deploying to a test environment, and anything the project rules say needs no asking.
- Ask first only about: things that cannot be undone (deleting data, force-pushing, changing production data), anything outward-facing (sending messages, posts or emails to other people on my behalf), anything that costs money, product direction (whether to do something, what it should look like, changing the goal or the completion criteria), and whatever the project rules say to ask about. Where the project rules differ from this, follow the project rules.
- When I say I'm going to bed or "keep going", keep working and stop only when you are truly stuck.
When you need me to decide things, put one line per decision above the 📌 line (not inside a code block):
`❓ <what I need to decide>（<ID>）｜<option 1>｜<option 2>｜卡：<task blocked until this is decided, optional>｜荐：<the option you recommend, copied exactly>，<why; what happens if I pick another>`
Put an ID in brackets at the end of the question, such as (Q-031); use the project's own question IDs if it has them, otherwise number from Q-1. When you ask the same thing again, keep the ID and the wording. The 卡： ("blocked:") and 荐： ("recommended:") parts are not options; leave 卡： out when nothing is blocked. Write 荐： when you are confident: the dashboard marks the recommended option and shows the reason under the question. If you are unsure, leave it out rather than force one.
Keep each option short enough to stand as my answer, at most 6; leave them out when there are no fixed choices. These lines are the list itself; don't repeat it in the body. I will answer one per line as `1. <decision>：<my pick>`, with 「待定」 for anything I haven't decided. The dashboard remembers what I haven't answered, so you don't need to repeat those in your next reply; list one again only to chase it, with the same ID.
For anything you decided without asking me that I might see differently (which approach you took, how you defined something, a step you skipped), put one line each above the 📌 line (not inside a code block):
`🧭 <what you decided>｜<why>`
At most 3 lines; skip the small stuff. The dashboard keeps them in the session's details, and I will come back to you if I disagree.
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
| `feishu_doc_titles` | `true` | Look up the real titles of Feishu docs in Outputs with `lark-cli`, as you; `false` keeps the AI's own wording |
| `gitlab_mr_state` | `true` | Look up the titles and merge status of GitLab MRs the AI gave you with `glab`, as you; `false` turns it off |

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

Stored in the install directory (none of it goes into git; `data/` and `logs/` are readable only by your own account): `data/ledger.db` (session status, your actions, topics, your messages, goals, outputs), `data/images/` (images, a few hundred take one or two hundred MB), `logs/`, `config.json`.

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
