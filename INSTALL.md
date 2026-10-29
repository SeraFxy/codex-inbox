# 安装 codex-inbox（给 AI 代理看的步骤）

用户让你安装 codex-inbox 时，按下面的顺序一步步做完。每一步都写了确切命令、怎么确认成功、失败了怎么办。

- 标「**停下来问用户**」的地方，先把问题和影响讲清楚，等用户回答再继续。
- 标「**要用户本人做**」的事（浏览器里授权、系统弹窗点允许），告诉用户具体点哪里，等用户说做完了再继续。
- 不要用 `sudo`，不要改本仓库的代码，不要删用户已有的文件或目录。命令失败时，把报错原文（最后 20 行以内）告诉用户。
- 默认装到 `~/codex-inbox`。用户指定了别的目录，就把下面命令里的 `~/codex-inbox` 都换掉，但不能放在「桌面」「文稿」「下载」里（macOS 不让后台服务读这几个目录）。
- 每做完一步，用一两句话告诉用户结果。

## 0. 前置检查

```sh
uname -s
/usr/bin/python3 -c 'import sys; print(sys.version)'
git --version
```

- 成功：第一行是 `Darwin`，Python 是 3.9 或更高，git 打印了版本号。
- 不是 `Darwin`：停下，告诉用户 codex-inbox 只支持 macOS。
- Python 或 git 报错、或弹出「需要命令行开发者工具」：**要用户本人做**：在弹窗里点「安装」，或在终端运行 `xcode-select --install`，等几分钟装完再告诉你。装好后重新跑这三条命令。

## 1. 下载到 ~/codex-inbox

```sh
if [ -d ~/codex-inbox/.git ]; then git -C ~/codex-inbox pull --ff-only
elif [ -e ~/codex-inbox ]; then echo "~/codex-inbox 已存在，但不是 git 仓库"
else git clone https://github.com/SeraFxy/codex-inbox.git ~/codex-inbox
fi
```

- 成功：`~/codex-inbox/ledger.py` 存在。已经装过的，这一步就是更新代码，后面照常往下走。
- 「已存在，但不是 git 仓库」：**停下来问用户**这个目录是什么、能不能换个目录名（如 `~/codex-inbox-app`）。不要删它。
- `git pull` 失败（多半是用户改过代码）：把报错告诉用户，不要 `reset`、`stash` 或强制覆盖。
- 用户之前已经把仓库 clone 到了桌面、文稿或下载里：不要在那里安装，照样按上面的命令装到 `~/codex-inbox`。

## 2. 安装看板服务

先告诉用户：「马上会弹一条 Mac 通知『AI 任务台账已就绪』。如果系统问是否允许『AI 任务台账』发送通知，请点允许，以后的到点提醒靠它。」然后运行：

```sh
~/codex-inbox/install.sh
```

- 成功：输出里有 `已启动：http://127.0.0.1:47800`（端口以输出为准）。
- **要用户本人做**：点允许通知。问用户有没有看到那条通知；没看到，请用户打开「系统设置 → 通知 → AI 任务台账」，打开「允许通知」。通知权限没法用命令检测，只能问用户。
- 输出「不能在这里安装」：说明你运行的是桌面、文稿或下载里的那一份，回到第 1 步，改用 `~/codex-inbox`。
- 输出「服务还没起来」：等 10 秒运行 `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:47800/`，应为 `200`。还不行就看 `tail -20 ~/codex-inbox/logs/launchd.err`。里面有 `Address already in use`：**停下来问用户**要不要换端口；同意就把 `~/codex-inbox/config.json` 里的 `"port"` 改成别的数（如 `47801`），再运行一次 `install.sh`。
- 输出「通知小程序没建成」：看板照常能用，只是通知改由系统脚本弹出。把 `tail -20 ~/codex-inbox/logs/notifier-build.log` 告诉用户。

## 3. 加状态行规则

看板靠 AI 每条回复最后一行的 📌 状态行来分组，所以要把规则加进 Codex 和 Claude Code 的全局规则文件。先预览：

```sh
~/codex-inbox/ledger rules show
```

**停下来问用户**，把下面这段话和预览结果告诉用户，同意了再加：

> 我会往下面两个文件的末尾加一段「任务台账状态行」规则，前后用 `<!-- codex-inbox:status-line:start -->` / `<!-- codex-inbox:status-line:end -->` 标记包起来，以后可以一条命令更新或删掉：
> - `~/.codex/AGENTS.md`（Codex 的全局规则；没装 Codex 就不加）
> - `~/.claude/CLAUDE.md`（Claude Code 的全局规则；没装 Claude Code 就不加）
>
> 文件里已经有你自己写的状态行规则的话会跳过，不会重复加。文件的其他内容不动。

```sh
~/codex-inbox/ledger rules install
```

- 成功：每个文件打印一行结果：已新建 / 已在末尾加上 / 已原地更新 / 已是最新 / 没装就不加。
- 「跳过：文件里已经有你手写的状态行规则」：告诉用户可以继续用自己的版本；想换成自动维护的，请他删掉手写的那段再运行 `rules install`。不要替他删。
- 「跳过：codex-inbox 标记不成对」：把文件路径告诉用户，请他删掉残缺的标记行后再运行。
- 告诉用户：**规则只对新开的会话生效**，正在跑的会话要重开才会写状态行。包括你自己这个会话。
- 想撤销：`~/codex-inbox/ledger rules remove`（只删标记之间的内容）。

## 4. 飞书每日汇总（可选）

**停下来问用户**：要不要每天早上把看板汇总私信到飞书？需要：

- 一个飞书账号；
- 本机装飞书命令行工具 lark-cli（npm 包，需要 Node.js）；
- 用户在浏览器里建一个飞书应用、再授权登录（各点几下）。

不用飞书就跳到第 5 步。要的话：

1. 看现在卡在哪一步（只读，不写文件也不发消息）：

   ```sh
   ~/codex-inbox/ledger feishu-setup --dry-run
   ```

   它会打印「没装 lark-cli」「还没建飞书应用」「还没登录」之一和对应的下一步命令；都齐了就打印会写入的 open_id 和会发的测试消息。按下面对应的小步做，每做完一步再跑一次这条命令。

2. 没装 lark-cli：

   ```sh
   npm install -g @larksuite/cli
   ```

   没有 `npm`：**停下来问用户**怎么装 Node.js：有 Homebrew 的可以 `brew install node`，否则请用户到 <https://nodejs.org> 下载安装包（**要用户本人做**）。装好后确认 `lark-cli --version` 能打印版本。

3. 还没建飞书应用：在**后台**运行下面的命令（它会一直等到用户在浏览器里建好应用）：

   ```sh
   lark-cli config init --new
   ```

   从它的输出里找到网址发给用户。**要用户本人做**：打开网址，按页面提示建好应用。用户说好了，确认命令已经结束，再跑一次第 1 小步。

4. 还没登录：

   ```sh
   lark-cli auth login
   ```

   它也会等用户在浏览器里授权，同样放到后台跑、把网址发给用户。你只能在回合结束时给用户发消息的话，改用 `lark-cli auth login --no-wait --json` 拿到网址和 device code 发给用户，用户授权后运行 `lark-cli auth login --device-code <上一步拿到的 device code>`。

5. 都齐了：

   ```sh
   ~/codex-inbox/ledger feishu-setup
   ```

   它把 lark-cli 当前登录的人的 open_id 写进 `~/codex-inbox/config.json`（其他配置不动），并发一条标着【示意】的测试汇总。问用户飞书里有没有收到。

6. 输出里有「下一步：运行 …install.sh 重启看板服务」时照做（服务只在启动时读配置）。

- 测试汇总发送失败：把报错告诉用户，运行 `lark-cli doctor` 看体检结果。用机器人身份发（默认）时，飞书应用要开「机器人」能力、有给用户发消息的权限，且用户在应用的可用范围内；这些要用户在飞书开放平台里改。

## 5. cmux 定时唤醒（可选）

**停下来问用户**：你用 cmux 终端跑 Claude Code 吗？要不要让看板能跳到 cmux 里的会话，并让 Claude 会话能用 `ledger schedule` 到点自己继续（比如晚上发版）？

同时说明安全影响：

> 要把 cmux 的「套接字控制模式」改成密码模式，密码存在 `~/.config/cmux/cmux.json`，只有你的账号能读。改了以后，这台 Mac 上知道这个密码的程序都能控制 cmux（看终端里的内容、往会话里打字）。不改的话，只是看板不能跳到 cmux、不能定时唤醒，其他功能不受影响。Codex 会话本来就用不了定时唤醒，用状态行「等时间点」。

不用 cmux、或用户不同意：跳过。用户明确同意后：

```sh
~/codex-inbox/ledger cmux-setup --dry-run
~/codex-inbox/ledger cmux-setup
```

- 成功：输出「已改成密码模式」（或「已经是密码模式」）、`cmux workspace list 通过`，看板服务在跑时还有「看板服务能控制 cmux 了」。改之前会把原文件备份成 `cmux.json.bak-<时间>`。
- 「没装 cmux」：跳过这一步。
- 「不是严格的 JSON（可能带注释）」：它不会硬改。按它打印的两种办法之一做：请用户在「cmux 设置 → 自动化」里改（**要用户本人做**）；或者经用户同意由你编辑这个文件，先 `cp ~/.config/cmux/cmux.json ~/.config/cmux/cmux.json.bak`，改完运行打印出来的 `config validate` 和 `reload-config` 两条命令。然后再运行一次 `~/codex-inbox/ledger cmux-setup` 确认。
- 「看板服务还是控制不了 cmux」：确认 cmux 开着，运行 `~/codex-inbox/ledger doctor` 看「cmux 定时唤醒」一项。

## 6. 体检

```sh
~/codex-inbox/ledger doctor
```

它只读、不改任何东西，逐项打印：

- ✅ 正常；❌ 要处理，按下面的「下一步」做；➖ 可选功能没启用，用户没选就不用管；⚠️ 提示，转述给用户，看情况处理。
- 最后一行是「必做项全部通过」才算装好（退出码 0）。有 ❌ 就按「下一步」做完再运行 doctor，直到必做项全过。同一项连着两次过不了，停下来把输出告诉用户。
- 「时区」出现 ⚠️：**停下来问用户**平时按哪个时区安排工作，按他的回答保留或把 `config.json` 的 `utc_offset_hours` 改成 `null`，再运行 `install.sh`。
- 「项目目录」是 ➖：问用户代码一般放在哪。不是 `~/Desktop/<项目>` 或 `~/<项目>` 这种结构的（如 `~/code/<项目>`），经用户同意把父目录写进 `config.json` 的 `project_parents`（如 `["~/code"]`），再运行 `install.sh`。
- 要机器可读的结果：`~/codex-inbox/ledger doctor --json`。

## 7. 收尾：告诉用户

用几句话告诉用户：

- 看板地址：`http://127.0.0.1:47800`（以 doctor 最后一行为准），建议固定在浏览器标签栏。
- 还剩哪些要本人做的事，比如：没收到通知的去「系统设置 → 通知 → AI 任务台账」打开；飞书或 cmux 的授权还没做完的；**正在跑的会话要重开才会写状态行**。
- 以后更新：`git -C ~/codex-inbox pull && ~/codex-inbox/install.sh && ~/codex-inbox/ledger rules install`。
- 卸载：`~/codex-inbox/uninstall.sh` 停掉服务；`~/codex-inbox/ledger rules remove` 删掉加进去的规则；不要数据了再删掉 `~/codex-inbox` 整个目录。
