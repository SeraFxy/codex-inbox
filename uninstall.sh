#!/bin/sh
# 停止并移除开机自启。代码和 data/ 里的数据都保留，不想要了直接删整个目录。
DIR="$(cd "$(dirname "$0")" && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
GUI="gui/$(id -u)"
found=0
for f in "$AGENTS"/*.plist; do
  [ -f "$f" ] || continue
  if grep -q "<string>$DIR/ledger.py</string>" "$f"; then
    old=$(/usr/libexec/PlistBuddy -c "Print :Label" "$f" 2>/dev/null || true)
    [ -n "$old" ] && launchctl bootout "$GUI/$old" 2>/dev/null || true
    rm -f "$f"
    found=1
  fi
done
[ "$found" = 1 ] && echo "已停止并移除后台服务" || echo "没找到指向 $DIR 的后台服务"
