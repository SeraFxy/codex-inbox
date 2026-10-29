#!/bin/sh
# 维护者用：把本仓库已提交的代码部署到安装目录，再用那边的 install.sh 重启服务。直接装在仓库目录里的用户用不到。
# 部署前核对：安装目录正在跑的版本（.deployed 里记的提交）必须已经包含在这次要部署的版本里；
# 不包含说明别的分支或工作目录部署过、这边还没合并，直接覆盖会把那边的改动冲掉。
#   ./deploy.sh [安装目录]               不写目录就部署到开机自启服务所在的目录
#   ./deploy.sh --no-restart [安装目录]  只同步代码，不重启服务
#   ./deploy.sh --init [安装目录]        第一次部署、安装目录又和这次的版本不一致时，确认可以覆盖后用它
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
INIT=; RESTART=1
while [ $# -gt 0 ]; do
  case "$1" in
    --init) INIT=1 ;;
    --no-restart) RESTART= ;;
    -*) echo "不认识的参数：$1"; exit 1 ;;
    *) DEST="$1" ;;
  esac
  shift
done

if [ -z "$DEST" ]; then
  PLIST="$HOME/Library/LaunchAgents/${CODEX_INBOX_LABEL:-local.codex-inbox}.plist"
  py=$(/usr/libexec/PlistBuddy -c "Print :ProgramArguments:1" "$PLIST" 2>/dev/null || true)
  [ -n "$py" ] || { echo "找不到开机自启服务（${PLIST}），请写明安装目录：./deploy.sh <安装目录>"; exit 1; }
  DEST=$(dirname "$py")
fi
[ -d "$DEST" ] || { echo "安装目录不存在：$DEST"; exit 1; }
DEST="$(cd "$DEST" && pwd)"
[ "$DEST" != "$SRC" ] || { echo "安装目录就是这个仓库，直接运行 ./install.sh"; exit 1; }

cd "$SRC"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "要在 git 仓库里运行"; exit 1; }
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "部署被拦下：有没提交的改动。只部署已提交的代码，先提交（在自己的分支上提交 WIP 也行）再部署："
  git status --short --untracked-files=no | head -20
  exit 1
fi
HEAD=$(git rev-parse HEAD)
BRANCH=$(git rev-parse --abbrev-ref HEAD)

# 两个会话同时部署时只让一个进来
LOCK="$DEST/.deploy.lock"
mkdir "$LOCK" 2>/dev/null || { echo "部署被拦下：另一个部署正在进行（${LOCK}）。等它结束；确认没有部署在跑，就删掉这个目录再试。"; exit 1; }
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

REC="$DEST/.deployed"
if [ -f "$REC" ]; then
  prev=$(sed -n 's/^commit=//p' "$REC")
  if ! git cat-file -e "$prev^{commit}" 2>/dev/null || ! git merge-base --is-ancestor "$prev" HEAD; then
    echo "部署被拦下：安装目录正在跑的版本不在这次要部署的版本里，直接覆盖会冲掉那边的改动。正在跑的是："
    sed 's/^/  /' "$REC"
    echo "先把它合并进来再部署：git merge ${prev}（这个仓库里找不到它，就先去上面 source 那个目录提交）。"
    exit 1
  fi
else
  diff=$(git -c core.quotePath=false ls-files | while read -r f; do
    if [ -e "$DEST/$f" ] && ! cmp -s "$f" "$DEST/$f"; then echo "$f"; fi
  done)
  if [ -n "$diff" ] && [ -z "$INIT" ]; then
    echo "部署被拦下：安装目录没有部署记录，下面这些文件和这次要部署的版本不同，没法判断是不是别人部署的改动："
    echo "$diff" | sed 's/^/  /'
    echo "确认可以覆盖后运行：./deploy.sh --init $DEST"
    exit 1
  fi
fi

git ls-files -z | rsync -a --from0 --files-from=- ./ "$DEST/"
printf 'commit=%s\nbranch=%s\nsource=%s\ntime=%s\n' "$HEAD" "$BRANCH" "$SRC" "$(date '+%Y-%m-%d %H:%M:%S')" > "$REC"
echo "已部署 $BRANCH $(git rev-parse --short HEAD) → $DEST"
rmdir "$LOCK"; trap - EXIT
[ -n "$RESTART" ] && "$DEST/install.sh"
exit 0
