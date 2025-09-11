#!/bin/zsh
set -euo pipefail

APP_NAME="AmazonQDesktop"
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_PATH1="${ROOT_DIR}/dist/${APP_NAME}/${APP_NAME}.app"
APP_PATH2="${ROOT_DIR}/dist/${APP_NAME}.app"
DMG_NAME="${APP_NAME}-Installer.dmg"
WORK_DIR="${ROOT_DIR}/dmg-work"
VOLUME_NAME="$APP_NAME"
BACKGROUND_IMG_CANDIDATE="${ROOT_DIR}/images/dmg-background.png" # あれば使用
APP_ICON_ICNS="${ROOT_DIR}/images/AmazonQDeveloperGui.icns"      # あればボリュームアイコンに使用

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

# Applications エイリアスを作成
ln -s /Applications "$WORK_DIR/Applications"

# 背景フォルダ（Finderは .background 配下の画像をよく使う）
mkdir -p "$WORK_DIR/.background"
if [ -f "$BACKGROUND_IMG_CANDIDATE" ]; then
  cp "$BACKGROUND_IMG_CANDIDATE" "$WORK_DIR/.background/background.png"
  echo "[INFO] 背景画像を使用: $BACKGROUND_IMG_CANDIDATE"
else
  echo "[INFO] 背景画像が見つからないため既定の背景（無地）を使用します"
fi

# ボリュームアイコン（存在すれば設定）
if [ -f "$APP_ICON_ICNS" ]; then
  cp "$APP_ICON_ICNS" "$WORK_DIR/.VolumeIcon.icns"
  echo "[INFO] ボリュームアイコンを設定予定: $APP_ICON_ICNS"
fi

# 一旦、read/write の DMG を作成
cd "$WORK_DIR/.."
hdiutil create -volname "$VOLUME_NAME" -srcfolder "$WORK_DIR" -ov -format UDRW "${APP_NAME}-temp.dmg"

# マウントして Finder レイアウトを設定
MNT_DIR="$(mktemp -d)"
hdiutil attach -mountpoint "$MNT_DIR" -nobrowse -quiet "${APP_NAME}-temp.dmg"

# AppleScript で Finder ウィンドウの見た目を調整
osascript <<OSA
on run
  set dmgPath to POSIX file "$MNT_DIR"
  tell application "Finder"
    tell disk "$VOLUME_NAME"
      open
      set current view of container window to icon view
      set toolbar visible of container window to false
      set statusbar visible of container window to false
      set bounds of container window to {200, 200, 900, 600}
      delay 0.2
      tell icon view options of container window
        set arrangement to not arranged
        set icon size to 96
      end tell
      -- 背景画像があれば設定
      try
        if exists file ".background:background.png" then
          set background picture of container window to file ".background:background.png"
        end if
      end try
      -- アイコンの配置（ドラッグ方向を想定した並び）
      try
        set position of file "$APP_NAME.app" to {140, 200}
      end try
      try
        set position of file "Applications" to {520, 200}
      end try
      update without registering applications
      delay 0.5
      close
      open
      delay 0.2
    end tell
  end tell
end run
OSA

# ボリュームアイコン反映（SetFile があればカスタムアイコンフラグを立てる）
if command -v SetFile >/dev/null 2>&1; then
  SetFile -a C "$MNT_DIR" || true
fi

# .DS_Store を保存するために Finder の状態を確定
sync
sleep 1

# アンマウント
hdiutil detach "$MNT_DIR" -quiet || true
rmdir "$MNT_DIR" || true

# 圧縮して最終的な UDZO DMG を作成
rm -f "$DMG_NAME"
hdiutil convert "${APP_NAME}-temp.dmg" -format UDZO -imagekey zlib-level=9 -o "$DMG_NAME" >/dev/null
rm -f "${APP_NAME}-temp.dmg"

echo "[OK] DMG 作成完了: ${ROOT_DIR}/${DMG_NAME}"
