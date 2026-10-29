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

case "$DIR" in
  "$HOME/Desktop"*|"$HOME/Documents"*|"$HOME/Downloads"*)
    echo "请先把整个目录复制到别处（如 ~/codex-inbox）再运行安装：macOS 不让后台服务读桌面、文稿、下载里的文件。"
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
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -s -o /dev/null "http://127.0.0.1:$PORT/"; then
    echo "已启动：http://127.0.0.1:$PORT"
    exit 0
  fi
  sleep 1
done
echo "服务还没起来，稍等再打开 http://127.0.0.1:$PORT ；仍不行就看 $DIR/logs/launchd.err"
