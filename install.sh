#!/bin/sh
# 安装为开机自启的后台服务（再次运行 = 更新后重启服务）。
# 可以装在任意目录，但不要放在「桌面」「文稿」「下载」里：macOS 不让后台服务读这几个目录。
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="${CODEX_INBOX_LABEL:-local.codex-inbox}"
PYTHON=/usr/bin/python3
AGENTS="$HOME/Library/LaunchAgents"
PLIST="$AGENTS/$LABEL.plist"
GUI="gui/$(id -u)"

# 不自动搬家：已经在别处装过的话，自动复制会多装出一份没有数据的新看板
case "$DIR" in
  "$HOME/Desktop"*|"$HOME/Documents"*|"$HOME/Downloads"*)
    echo "不能在这里安装：macOS 不让后台服务读「桌面」「文稿」「下载」里的文件。"
    echo "把整个目录复制到 ~/codex-inbox，再运行那边的 install.sh（这里的文件不会被改动）："
    if [ -e "$HOME/codex-inbox" ]; then
      echo "  ~/codex-inbox 已经存在。如果它就是 codex-inbox，直接运行：~/codex-inbox/install.sh"
      echo "  否则换个目录名：cp -R \"$DIR\" ~/codex-inbox-app && ~/codex-inbox-app/install.sh"
    else
      echo "  cp -R \"$DIR\" ~/codex-inbox && ~/codex-inbox/install.sh"
      echo "或者重新下载一份："
      echo "  git clone https://github.com/SeraFxy/codex-inbox.git ~/codex-inbox && ~/codex-inbox/install.sh"
    fi
    echo "以后更新代码、运行 ledger 命令，都用新目录里的那一份。"
    exit 1 ;;
esac

mkdir -p "$DIR/data" "$DIR/logs" "$AGENTS"
chmod 700 "$DIR/data" "$DIR/logs"   # 里面是你的对话记录，只给自己的账号读
[ -f "$DIR/config.json" ] || cp "$DIR/config.example.json" "$DIR/config.json"

# 停掉指向本目录的旧服务（包括换过名字的旧版本），避免两个实例抢同一个端口
for f in "$AGENTS"/*.plist; do
  [ -f "$f" ] || continue
  if grep -q "<string>$DIR/ledger.py</string>" "$f"; then
    old=$(/usr/libexec/PlistBuddy -c "Print :Label" "$f" 2>/dev/null || true)
    [ -n "$old" ] && launchctl bootout "$GUI/$old" 2>/dev/null || true
    [ "$f" = "$PLIST" ] || rm -f "$f"
  fi
done
launchctl bootout "$GUI/$LABEL" 2>/dev/null || true

if ! CODEX_INBOX_LABEL="$LABEL" "$DIR/notifier/build.sh" >"$DIR/logs/notifier-build.log" 2>&1; then
  echo "通知小程序没建成（见 logs/notifier-build.log），通知会改用系统脚本弹出，点通知会打开「脚本编辑器」。"
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$DIR/ledger.py</string>
    <string>serve</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>Nice</key><integer>5</integer>
  <key>StandardOutPath</key><string>$DIR/logs/launchd.out</string>
  <key>StandardErrorPath</key><string>$DIR/logs/launchd.err</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>LANG</key><string>zh_CN.UTF-8</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
</dict>
</plist>
EOF
launchctl bootstrap "$GUI" "$PLIST"

PORT=$("$PYTHON" -c 'import json, sys
try:
    print(json.load(open(sys.argv[1])).get("port", 47800))
except Exception:
    print(47800)' "$DIR/config.json")
up=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -s -o /dev/null "http://127.0.0.1:$PORT/"; then
    up=1
    break
  fi
  sleep 1
done
if [ "$up" = 1 ]; then
  echo "已启动：http://127.0.0.1:$PORT"
else
  echo "服务还没起来，稍等再打开 http://127.0.0.1:$PORT ；仍不行就看 $DIR/logs/launchd.err"
fi

# 弹一条欢迎通知：第一次弹会触发 macOS 的通知授权提示（通知小程序读 data/notify.txt 的「标题\n正文」）
APP="$DIR/notifier/AI 任务台账.app"
if [ -d "$APP" ] && printf '%s\n%s' "AI 任务台账已就绪" "到点提醒会从这里弹出，点通知打开看板" > "$DIR/data/notify.txt" \
    && open -g "$APP" 2>/dev/null; then :
else
  osascript -e 'display notification "到点提醒会从这里弹出" with title "AI 任务台账已就绪"' 2>/dev/null || true
fi
echo "刚弹了一条通知「AI 任务台账已就绪」。如果弹出系统提示，请点允许；没看到通知就去「系统设置 → 通知 → AI 任务台账」打开。"
echo "下一步：运行 $DIR/ledger doctor"
