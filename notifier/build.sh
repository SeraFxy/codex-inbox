#!/bin/sh
# 编译通知小程序：由它弹 Mac 通知，点通知会打开看板。install.sh 会自动调用。
# 直接用 osascript 弹的通知，发送方是「脚本编辑器」，点了会打开脚本编辑器。
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$DIR/AI 任务台账.app"
BUNDLE_ID="${CODEX_INBOX_LABEL:-local.codex-inbox}.notifier"
P="$APP/Contents/Info.plist"

rm -rf "$APP"
osacompile -o "$APP" "$DIR/notify.applescript"
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$P"
/usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string $BUNDLE_ID" "$P" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $BUNDLE_ID" "$P"
# 用自带的机器人图标：去掉默认的资源目录图标，否则系统优先显示它
/usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$P" 2>/dev/null || true
rm -f "$APP/Contents/Resources/Assets.car"
cp "$DIR/icon/AppIcon.icns" "$APP/Contents/Resources/applet.icns"
codesign --force --deep --sign - "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" || true
echo "已生成 $APP（$BUNDLE_ID）"
