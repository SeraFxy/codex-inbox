# codex-inbox · AI 任务台账

同时开很多个 Codex / Claude Code 会话时，很容易忘了哪个在等你回话、哪个在等同事、哪个今晚要发版。codex-inbox 是一个跑在你 Mac 上的本地看板：它读这两个工具留在本机的对话记录，按「现在等谁」把会话分好组，到点弹通知，还把你说过的话、AI 给过的链接和你发过的图片另存一份。

English: [README.en.md](README.en.md)

*A local macOS dashboard for people who run many Codex and Claude Code sessions at once. It reads the transcripts both tools already keep on disk, groups sessions by who they are waiting on, notifies you when something is due, and keeps a searchable copy of what you asked. No AI calls, no tokens, nothing leaves your machine. The UI is in Chinese.*

名字里只写了 Codex 是为了好懂，**Codex（桌面版和 CLI）和 Claude Code 都支持**。

## 让 AI 帮你装

把下面这句话发给你的 Claude Code 或 Codex，它会按 [INSTALL.md](INSTALL.md) 把安装和设置做完，只在需要你本人动手时（浏览器里授权、系统弹窗点允许、同意改安全设置）停下来问你：

```text
帮我安装 codex-inbox：https://github.com/SeraFxy/codex-inbox ，按仓库里的 INSTALL.md 一步步做完
```

想自己动手，看下面的「安装」。

## 它能做什么

- **按「现在等谁」分组**：左边四个标签页：轮到你（现在需要你、待你确认完成）/ AI 在做 / 在等（等时间点、等同事）/ 待整理，点一条右边出详情。键盘 J / K 上下，O 打开会话，E 完成，S 推迟，⌘K 搜索；行可以拖动调顺序。分组依据是 AI 每条回复最后写的一行状态（见下面的「状态行」），没写状态的会话也会列出来，标成「状态不明」。
- **卡住了也能看出来**：AI 停在一个提问或待批准的操作上、你发的消息半小时没回、停在半路很久没动静，都会排到「现在需要你」。
- **到点提醒**：状态行带了将来的时间，就先放在「等时间点」，到点挪到「现在需要你」并弹 Mac 通知，点通知打开看板。每张卡可以推迟 15 分钟、1 小时、3 小时、到今晚、到明早或下周一。
- **待你决定**：AI 在回复里用 `❓ 事情｜选项｜选项` 列出要你拍板的事，展开卡片就能逐件点选项，看板拼成一条回复（没选的写「待定」，还能补几句）。点「复制并打开会话」，回复复制到剪贴板、会话也打开了，你自己按 ⌘V 回车发出去。看板不替你发：每发一条，AI 都要把整段对话重读一遍，什么时候发由你决定。
- **「我说过的话」**：你发给 Codex / Claude 的每一句话按会话归组，支持多关键词搜索、逐句复制。Claude Code 默认 30 天后删对话记录，这里另存一份纯文本（一年约 20MB）。
- **另存目标、链接和图片**：Codex 的 goal、状态行里的 🎯 目标、AI 回复里给过的链接（飞书文档、MR、GitHub 等，代码块和命令行里的不算）、你发给 AI 的图片（按内容去重）。原会话被删了也还在。
- **点「完成」不会弄丢**：完成的会话挪到最下面，你再和它说话，它会自动回到看板。
- **会话名和标签**：会话名优先用你在 cmux 终端的侧边栏里手动改的名字；项目短码可以改名字、加 emoji（项目一律灰色，颜色只用来表示状态）。
- **可选功能**（需要另装工具，没装就用不了，其他功能不受影响）：
  - 定时唤醒：在 cmux 终端里跑的 Claude 会话，可以用 `ledger schedule` 让它到点自己继续（比如晚上发版）。需要 cmux，且要在 cmux 设置里允许后台程序控制它（`ledger cmux-setup`，见「已知限制」）。
  - 飞书每日汇总：装了飞书命令行工具 lark-cli 并在配置里填了 `feishu_open_id`，每天早上把汇总私信给你。`ledger feishu-setup` 会替你填好并发一条测试。

全部是读本机文件，**不调用任何 AI，不花 token**。服务只监听 `127.0.0.1`，网络请求只有可选的飞书汇总。

## 安装

需要 macOS，用系统自带的 `/usr/bin/python3`（3.9 及以上），不需要装任何依赖。

```sh
git clone https://github.com/SeraFxy/codex-inbox.git ~/codex-inbox
~/codex-inbox/install.sh
```

然后打开 <http://127.0.0.1:47800>，建议固定在浏览器标签栏。装完运行 `~/codex-inbox/ledger doctor`，它逐项检查安装（只读），没装好的会告诉你下一步。

- **不要装在「桌面」「文稿」「下载」里**：macOS 不让后台服务读这几个目录，`install.sh` 会拦住并提示。
- 安装会注册一个开机自启的 launchd 服务（默认名 `local.codex-inbox`，可以用环境变量 `CODEX_INBOX_LABEL` 改），并编译一个叫「AI 任务台账」的通知小程序。装完会弹一条欢迎通知，macOS 问要不要允许它发通知时点允许。
- 更新代码后再运行一次 `install.sh` 就会重启服务；`uninstall.sh` 停止并移除服务，数据留在 `data/`。

## 让 AI 写状态行

看板靠 AI 每条回复最后一行的状态来分组。规则写在 [status-line.md](status-line.md)，装好后运行一次：

```sh
~/codex-inbox/ledger rules install
```

它把规则加到 `~/.codex/AGENTS.md`（Codex）和 `~/.claude/CLAUDE.md`（Claude Code）的末尾，前后用 `<!-- codex-inbox:status-line:start -->` / `<!-- codex-inbox:status-line:end -->` 标记包好，「已定时」要用的 `ledger schedule` 会换成你本机的完整路径。只给装了的工具加；文件里已经有你手写的状态行规则（含 `📌 <状态>`）就跳过。更新代码后再运行一次会原地更新；`ledger rules show` 预览，`ledger rules remove` 只删标记之间的内容。新开的会话才会按规则写。

手动加也行：把 status-line.md 开头说明注释以下的内容复制进上面两个文件，把 `{{LEDGER}}` 换成 `<安装目录>/ledger`。

## 配置

第一次安装会从 `config.example.json` 复制出 `config.json`，改完运行 `install.sh` 生效。可以写的项（都有默认值）：

| 项 | 默认 | 说明 |
|---|---|---|
| `port` | `47800` | 看板端口 |
| `scan_interval_min` | `15` | 后台多久读一次对话记录；看板网页开着时每分钟也会读一次 |
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
./ledger doctor              # 体检：逐项检查安装，给出下一步（只读）
./ledger rules install       # 加状态行规则（remove 删掉，show 预览）
./ledger feishu-setup        # 设置飞书每日汇总并发一条测试（--dry-run 只看不做）
./ledger cmux-setup          # 让看板服务能控制 cmux，定时唤醒要用（--dry-run 只看不做）
```

## 读了哪些文件、存了什么

| 来源 | 用途 |
|---|---|
| `~/.claude/projects/*/*.jsonl`、`~/.claude/history.jsonl` | Claude Code 会话、你发过的话和图片 |
| `~/.claude/sessions/*.json` | 开着的 Claude 会话此刻在跑、空闲，还是弹着批准框在等你 |
| `~/.codex/sessions/`、`~/.codex/*.sqlite`、`~/.codex/session_index.jsonl` | Codex 会话、标题、goal、你发过的话和图片 |
| `~/Library/Application Support/cmux/session-*.json` | cmux 侧边栏里改过的会话名（只读） |

存在安装目录里（都不进 git，`data/` 和 `logs/` 只有你自己的账号能读）：`data/ledger.db`（会话状态、你的操作、你说过的话、目标、链接）、`data/images/`（图片，几百张约一两百 MB）、`logs/`、`config.json`。

## 测试过的版本

在下面这套环境里实际用过；别的版本大概率也能用，但没测过：

- macOS 26.5，系统自带 Python 3.9
- Codex 桌面版（ChatGPT.app 26.1023.51421，内置 codex-cli 0.165.0-alpha.1.2）
- Claude Code 2.1.312

Codex 部分读的是它的内部文件，文件名里带版本号：`~/.codex/state_5.sqlite`（会话来源和标题）、`goals_1.sqlite`（goal）、`thread_history_1.sqlite`（你发过的话、AI 回复里的链接），以及 `session_index.jsonl` 和 `sessions/` 下的对话记录。Codex 升级后如果这些文件改了名字或结构，相应的功能（标题、goal、「我说过的话」里的 Codex 部分）可能读不到，日志里会有提示；看板分组靠的是 `sessions/` 下的对话记录，一般不受影响。

## 已知限制

- 只看最近 14 天有动静的会话；子代理、审查代理、Codex 自动任务线程和 `claude -p` 不进看板。
- 「打开会话」：Codex 会话用 `codex://threads/<id>` 打开；Claude 会话要跳到 cmux 里的对应窗口，但 cmux 默认只允许在 cmux 终端里启动的程序控制它，开机自启的看板服务会被拒绝，这时看板只会提示，不会替你另开一个会话；定时唤醒也受同样的限制。要放开，把 cmux 改成密码模式：在 `~/.config/cmux/cmux.json` 里加上 `"automation": {"socketControlMode": "password", "socketPassword": "<随便一串长密码>"}`，再运行 `cmux reload-config`（也可以在 cmux 设置 → 自动化里改「套接字控制模式」并设密码）。cmux 命令行会自己读这个密码，看板不用另外配置；在 cmux 终端里跑的程序不受影响。`ledger cmux-setup` 可以替你改（文件带注释时它只打印说明，不硬改）。
- Mac 睡眠时服务也在睡，通知和定时唤醒在醒来后补上；晚了超过 `wake_expire_hours` 的唤醒不再执行。
- 看板的读取接口没有设密码：网页打不开它（服务会核对来源），但这台 Mac 上的程序都能读到会话列表、你说过的话和图片。改动操作（标完成、推迟、改标签等）需要令牌。用你自己账号跑的程序本来就能直接读 `data/` 里的数据库，加密码也挡不住；如果这台 Mac 还有别人的登录账号，请留意这一点。

## 许可证

MIT，见 [LICENSE](LICENSE)。

## 文件

| 文件 | 用途 |
|---|---|
| `ledger.py`（`ledger` 是它的快捷方式） | 全部逻辑，只用 Python 标准库 |
| `board.html` | 看板页面 |
| `notifier/` | 通知小程序的源码（`notify.applescript`）、编译脚本和图标（按 Blink 视觉规范画的，源文件 `icon/icon.svg`） |
| `install.sh` / `uninstall.sh` | 安装 / 移除开机自启 |
| `status-line.md` | 状态行规则的唯一来源，`ledger rules install` 从这里读 |
| `INSTALL.md` | 给 AI 代理看的安装步骤 |
