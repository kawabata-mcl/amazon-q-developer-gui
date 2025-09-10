#!/bin/zsh
set -euo pipefail

APP_NAME="AmazonQDesktop"
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_PATH1="${ROOT_DIR}/dist/${APP_NAME}/${APP_NAME}.app"
APP_PATH2="${ROOT_DIR}/dist/${APP_NAME}.app"
DMG_NAME="${APP_NAME}-Installer.dmg"
WORK_DIR="${ROOT_DIR}/dmg-work"

# resolve .app
if [ -d "$APP_PATH1" ]; then
  APP_PATH="$APP_PATH1"
elif [ -d "$APP_PATH2" ]; then
  APP_PATH="$APP_PATH2"
else
  echo "[ERROR] .app が見つかりません。先に ./build-macos.sh を実行してください。" >&2
  exit 1
fi

echo "[INFO] using app: $APP_PATH"

# prepare folder
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"
cp -R "$APP_PATH" "$WORK_DIR/"

# create Applications alias
osascript <<'OSA'
set appsPath to POSIX path of "/Applications"
set dmgWork to POSIX file (do shell script "pwd")
OSA

# Instead of AppleScript, create symlink for Applications
ln -s /Applications "$WORK_DIR/Applications"

# create DMG
cd "$WORK_DIR/.."
hdiutil create -volname "$APP_NAME" -srcfolder "$WORK_DIR" -ov -format UDZO "$DMG_NAME"
echo "[OK] DMG 作成完了: ${ROOT_DIR}/${DMG_NAME}"
