# codex-inbox · AI 任务台账

同时开很多个 Codex / Claude Code 会话时，很容易忘了哪个在等你回话、哪个在等同事、哪个今晚要发版。codex-inbox 是一个跑在你 Mac 上的本地看板：它读这两个工具留在本机的对话记录，按「现在等谁」把会话分好组，到点弹通知，还把你说过的话、AI 给过的链接和你发过的图片另存一份。

*A local macOS dashboard for people who run many Codex and Claude Code sessions at once. It reads the transcripts both tools already keep on disk, groups sessions by who they are waiting on, notifies you when something is due, and keeps a searchable copy of what you asked. No AI calls, no tokens, nothing leaves your machine. The UI is in Chinese.*

名字里只写了 Codex 是为了好懂，**Codex（桌面版和 CLI）和 Claude Code 都支持**。

## 它能做什么

- **按「现在等谁」分组**：现在需要你 / AI 在做 / 待你确认完成 / 等时间点 / 等同事 / 待整理。分组依据是 AI 每条回复最后写的一行状态（见下面的「状态行」），没写状态的会话也会列出来，标成「状态不明」。
- **卡住了也能看出来**：AI 停在一个提问或待批准的操作上、你发的消息半小时没回、停在半路很久没动静，都会排到「现在需要你」。
- **到点提醒**：状态行带了将来的时间，就先放在「等时间点」，到点挪到「现在需要你」并弹 Mac 通知，点通知打开看板。每张卡可以推迟 15 分钟、1 小时、3 小时、到今晚、到明早或下周一。
- **「我说过的话」**：你发给 Codex / Claude 的每一句话按会话归组，支持多关键词搜索、逐句复制。Claude Code 默认 30 天后删对话记录，这里另存一份纯文本（一年约 20MB）。
- **另存目标、链接和图片**：Codex 的 goal、状态行里的 🎯 目标、AI 回复里给过的链接（飞书文档、MR、GitHub 等，代码块和命令行里的不算）、你发给 AI 的图片（按内容去重）。原会话被删了也还在。
- **点「完成」不会弄丢**：完成的会话挪到最下面，你再和它说话，它会自动回到看板。
- **会话名和标签**：会话名优先用你在 cmux 终端的侧边栏里手动改的名字；项目标签可以改名字、加 emoji、换颜色。
- **可选**：在 cmux 里的 Claude 会话，可以用 `ledger schedule` 让它到点自己继续（比如晚上发版）；装了飞书命令行工具 lark-cli 的话，每天早上把汇总私信给你。

全部是读本机文件，**不调用任何 AI，不花 token**。服务只监听 `127.0.0.1`，网络请求只有可选的飞书汇总。

## 安装

需要 macOS，用系统自带的 `/usr/bin/python3`（3.9 及以上），不需要装任何依赖。

```sh
git clone https://github.com/SeraFxy/codex-inbox.git ~/codex-inbox
~/codex-inbox/install.sh
```

然后打开 <http://127.0.0.1:47800>，建议固定在浏览器标签栏。

- **不要装在「桌面」「文稿」「下载」里**：macOS 不让后台服务读这几个目录，`install.sh` 会拦住并提示。
- 安装会注册一个开机自启的 launchd 服务（默认名 `local.codex-inbox`，可以用环境变量 `CODEX_INBOX_LABEL` 改），并编译一个叫「AI 任务台账」的通知小程序。第一次弹通知时，macOS 会问要不要允许它发通知。
- 更新代码后再运行一次 `install.sh` 就会重启服务；`uninstall.sh` 停止并移除服务，数据留在 `data/`。

## 让 AI 写状态行

看板靠 AI 每条回复最后一行的状态来分组。把下面这段加进 `~/.codex/AGENTS.md`（Codex）和 `~/.claude/CLAUDE.md`（Claude Code）：

```markdown
## 任务台账状态行
每条直接回复我的消息，最后一行写状态（作为子代理或非交互运行时不写）：
`📌 <状态>｜<一句话>｜<时间，可省>`
状态只用六个词：等你 / 等同事 / 等时间点 / 已定时 / AI在做 / 待你确认完成。
- 等你：写我下一步要做的具体动作，不写「请查看」这类空话。
- 等同事：写成「等同事（姓名）」，时间写约定再跟进的时间。
- 等时间点：需要我本人到点去做的事，时间必填。
- 已定时：你已经安排自己到点继续，时间必填（目前只有 cmux 里的 Claude 会话支持）。
- AI在做：你还在后台跑，暂时不需要我。
- 待你确认完成：已达到完成标准，一句话写证据；不要自己写「已完成」。
- 时间写成 `YYYY-MM-DD HH:MM`（和看板同一个时区）。回复正文里不要再出现 📌 和 🎯。
任务的第一条回复，以及目标或完成标准变了的时候，在 📌 上面加一行：
`🎯 目标：<一句话>｜完成标准：<做到什么算完>`
```

Claude Code 会话要用「已定时」，还需要告诉它用这个命令安排唤醒（把路径换成你的安装目录）：

```sh
~/codex-inbox/ledger schedule --at "YYYY-MM-DD HH:MM" --note "<看板上的一句话>" --prompt "<到点要做什么>"
```

## 配置

第一次安装会从 `config.example.json` 复制出 `config.json`，改完运行 `install.sh` 生效。可以写的项（都有默认值）：

| 项 | 默认 | 说明 |
|---|---|---|
| `port` | `47800` | 看板端口 |
| `scan_interval_min` | `15` | 后台多久读一次对话记录；打开看板时也会读 |
| `window_days` | `14` | 只看最近多少天有动静的会话 |
| `stale_hours` | `48` | 多久没动静算「待整理」 |
| `interrupted_min` | `30` | 停在半路多久算「可能卡住了」 |
| `digest_time` | `"09:00"` | 飞书汇总的时间 |
| `feishu_open_id` | 空 | 你的飞书 open_id；空着就不发汇总 |
| `feishu_as` | `"bot"` | 用 lark-cli 的哪种身份发 |
| `wake_expire_hours` | `3` | 定时唤醒晚了多久就放弃（比如 Mac 一直在睡） |
| `utc_offset_hours` | 空 | 看板按哪个时区算时间，空就用本机时区。Mac 的时区和你安排工作的时区不一样时填，比如人在国外但按北京时间工作就填 `8`。状态行和 `ledger schedule` 里的时间也按它理解 |
| `project_parents` | `[]` | 这些目录下的每个子目录算一个项目，比如 `["~/code", "~/work/repos"]`。不填时按 `~/Desktop/<项目>`、`~/<项目>` 推断 |

## 常用命令

```sh
./install.sh                 # 安装或重启服务
./uninstall.sh               # 停止并移除服务
./ledger scan -v             # 手动扫一次，打印各组
./ledger schedules           # 最近的定时唤醒
./ledger cancel <编号>        # 取消定时唤醒
./ledger digest              # 预览飞书汇总（--send 发送）
```

## 读了哪些文件、存了什么

| 来源 | 用途 |
|---|---|
| `~/.claude/projects/*/*.jsonl`、`~/.claude/history.jsonl` | Claude Code 会话、你发过的话和图片 |
| `~/.codex/sessions/`、`~/.codex/*.sqlite`、`~/.codex/session_index.jsonl` | Codex 会话、标题、goal、你发过的话和图片 |
| `~/Library/Application Support/cmux/session-*.json` | cmux 侧边栏里改过的会话名（只读） |

存在安装目录里（都不进 git）：`data/ledger.db`（会话状态、你的操作、你说过的话、目标、链接）、`data/images/`（图片，几百张约一两百 MB）、`logs/`、`config.json`。

## 已知限制

- 只看最近 14 天有动静的会话；子代理、审查代理、Codex 自动任务线程和 `claude -p` 不进看板。
- 「打开会话」：Codex 会话用 `codex://threads/<id>` 打开；Claude 会话要跳到 cmux 里的对应窗口，但 cmux 默认只允许在 cmux 里启动的程序控制它，开机自启的服务会被拒绝，这时看板只会提示，不会替你另开一个会话。要放开，需要在 cmux 设置里把 socket 访问方式改成密码模式。定时唤醒也受同样的限制。
- Mac 睡眠时服务也在睡，通知和定时唤醒在醒来后补上；晚了超过 `wake_expire_hours` 的唤醒不再执行。

## 许可证

MIT，见 [LICENSE](LICENSE)。

## 文件

| 文件 | 用途 |
|---|---|
| `ledger.py`（`ledger` 是它的快捷方式） | 全部逻辑，只用 Python 标准库 |
| `board.html` | 看板页面 |
| `notifier/` | 通知小程序的源码（`notify.applescript`）、编译脚本和机器人图标（图标用 Codex 的图片生成画的） |
| `install.sh` / `uninstall.sh` | 安装 / 移除开机自启 |
