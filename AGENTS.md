# 给 AI 编程代理的约定

在这个仓库里改代码的 Claude Code / Codex 会话，开工前先读这一页。

## 提交和推送

- 不要强推（`git push --force`、`--force-with-lease`），也不要改写已经推上去的提交（rebase、amend 等）。GitHub 会记下被覆盖的旧提交号，旧内容照样能被打开。
- 提交前确认改动里没有个人信息：真实的本机路径（`/Users/<名字>/…`）、公司或客户名、飞书 open_id、密码、令牌、对话内容。文档和示例里一律用占位符，如 `<你的 open_id>`。
- 不要提交 `data/`、`logs/`、`config.json`。它们已经写进 .gitignore，也不要用 `git add -f` 强行加。
- 可能有几个会话同时在这个目录里改代码：提交时只 `git add` 你自己改的文件，不要用 `git add -A` 把别人改到一半的东西一起提交。
- 维护者的本机装了 git 钩子：提交时扫个人信息，推送时拦强推。被拦下就改内容，不要用 `--no-verify` 绕过。

## 改完怎么生效

- 看板服务不能从「桌面」「文稿」「下载」里跑，macOS 不让后台服务读这几个目录。所以仓库放在这些目录时，服务实际跑在另一个安装目录。
- 安装目录在哪：看 `~/Library/LaunchAgents/local.codex-inbox.plist` 里 `ledger.py` 的路径。
- 改完把代码同步过去，再用那边的 `install.sh` 重启：

  ```sh
  rsync -a --exclude-from=.gitignore --exclude=.git ./ <安装目录>/ && <安装目录>/install.sh
  ```

  同步不会碰安装目录里的 `data/`、`logs/` 和 `config.json`。
