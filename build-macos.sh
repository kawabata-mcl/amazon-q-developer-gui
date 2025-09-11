#!/bin/zsh
set -euo pipefail

APP_NAME="AmazonQDesktop"
ENTRY="app.py"
ICON_PATH=""

# デフォルトの Amazon Q 公式 DMG（macOS用）
DEFAULT_Q_DMG_URL="https://desktop-release.q.us-east-1.amazonaws.com/latest/Amazon%20Q.dmg"

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENDOR_DIR="${ROOT_DIR}/vendor"
VENDOR_Q="${VENDOR_DIR}/q"
RESOLVED_Q=""
TARGET_ARCH="$(uname -m)"   # arm64 / x86_64

# 指定 PNG から .icns を生成して ICON_PATH に設定
_ensure_icns() {
  local png_src="${ROOT_DIR}/images/AmazonQDeveloperGui.png"
  local icns_out="${ROOT_DIR}/images/AmazonQDeveloperGui.icns"
  if [ -f "$icns_out" ]; then
    ICON_PATH="$icns_out"
    return 0
  fi
  if [ ! -f "$png_src" ]; then
    echo "[WARN] アイコン PNG が見つかりませんでした: $png_src"
    return 0
  fi
  # sips と iconutil がある環境のみ生成
  if ! command -v sips >/dev/null 2>&1 || ! command -v iconutil >/dev/null 2>&1; then
    echo "[WARN] sips または iconutil が見つからないため .icns 生成をスキップします (macOS での実行を想定)。"
    return 0
  fi
  local setdir
  setdir="$(mktemp -d)" || return 0
  trap "rm -rf '$setdir'" RETURN
  setdir="$setdir/AmazonQDeveloperGui.iconset"
  mkdir -p "$setdir"
  # 必要各サイズへリサイズ
  local sizes=(16 32 64 128 256 512 1024)
  for sz in "${sizes[@]}"; do
    sips -z "$sz" "$sz" "$png_src" --out "$setdir/icon_${sz}x${sz}.png" >/dev/null
    local dsz=$((sz*2))
    sips -z "$dsz" "$dsz" "$png_src" --out "$setdir/icon_${sz}x${sz}@2x.png" >/dev/null
  done
  iconutil -c icns "$setdir" -o "$icns_out"
  if [ -f "$icns_out" ]; then
    echo "[INFO] .icns 生成: $icns_out"
    ICON_PATH="$icns_out"
  else
    echo "[WARN] .icns 生成に失敗しました"
  fi
}

_arch_ok() {
  local bin="$1"
  # 'file' 出力に対象アーキが含まれていればOK（universal2なども許容）
  local info
  info="$(file -b "$bin" || true)"
  case "$TARGET_ARCH" in
    arm64)
      [[ "$info" == *"arm64"* ]] && return 0 || return 1 ;;
    x86_64)
      [[ "$info" == *"x86_64"* ]] && return 0 || return 1 ;;
    *)
      # 未知のアーキはチェック緩め（将来拡張）
      return 0 ;;
  esac
}

_download_and_extract_q() {
  local url="$1"
  mkdir -p "$VENDOR_DIR"
  local tmpdir
  tmpdir="$(mktemp -d)"
  # trapで直接パスを埋め込んで未定義参照を回避
  trap "rm -rf '$tmpdir'" EXIT

  echo "[INFO] downloading: $url"
  local lower
  lower="${url:l}"
  if [[ "$lower" == *.dmg ]]; then
    local dmg="$tmpdir/qsrc.dmg"
    curl -fsSL "$url" -o "$dmg"
    local mnt="${tmpdir}/mnt"
    mkdir -p "$mnt"
    hdiutil attach -nobrowse -quiet -mountpoint "$mnt" "$dmg"
    # 優先: App バンドル配下の典型パス、次点: 任意の実行可能 'q'
    local cand=""
    for p in \
      "$(/usr/bin/find "$mnt" -path '*/Contents/MacOS/q' -type f -perm -111 2>/dev/null | head -n1)" \
      "$(/usr/bin/find "$mnt" -type f -name q -perm -111 2>/dev/null | head -n1)" ; do
      if [ -n "$p" ]; then cand="$p"; break; fi
    done
    if [ -z "$cand" ]; then
      echo "[ERROR] DMG 内に q バイナリが見つかりませんでした" >&2
      hdiutil detach "$mnt" -quiet || true
      return 1
    fi
    cp "$cand" "$VENDOR_Q"
    chmod +x "$VENDOR_Q"
    hdiutil detach "$mnt" -quiet || true
  elif [[ "$lower" == *.zip ]]; then
    local zipf="$tmpdir/qsrc.zip"
    curl -fsSL "$url" -o "$zipf"
    (cd "$tmpdir" && unzip -q "$zipf")
    local cand
    # 典型パス優先で探索
    cand="$(/usr/bin/find "$tmpdir" -path '*/Contents/MacOS/q' -type f -perm -111 2>/dev/null | head -n1 || true)"
    if [ -z "$cand" ]; then
      cand="$(/usr/bin/find "$tmpdir" -type f -name q -perm -111 2>/dev/null | head -n1 || true)"
    fi
    if [ -z "$cand" ]; then
      echo "[ERROR] ZIP 内に q バイナリが見つかりませんでした" >&2
      return 1
    fi
    cp "$cand" "$VENDOR_Q"
    chmod +x "$VENDOR_Q"
  else
    # 生バイナリ
    curl -fsSL "$url" -o "$VENDOR_Q"
    chmod +x "$VENDOR_Q"
  fi
}

# 取得順: Q_BINARY > vendor/q > Q_BINARY_URL > DEFAULT_Q_DMG_URL
if [ -n "${Q_BINARY:-}" ] && [ -x "${Q_BINARY}" ]; then
  RESOLVED_Q="${Q_BINARY}"
elif [ -f "$VENDOR_Q" ]; then
  chmod +x "$VENDOR_Q" || true
  RESOLVED_Q="$VENDOR_Q"
else
  if [ -n "${Q_BINARY_URL:-}" ]; then
    _download_and_extract_q "$Q_BINARY_URL"
  else
    echo "[INFO] Q_BINARY/Q_BINARY_URL/vendor/q が未指定のため、公式DMGから自動取得します"
    _download_and_extract_q "$DEFAULT_Q_DMG_URL"
  fi
  RESOLVED_Q="$VENDOR_Q"
fi

if [ ! -x "$RESOLVED_Q" ]; then
  echo "[ERROR] q が実行可能ではありません: $RESOLVED_Q" >&2
  exit 1
fi

if ! _arch_ok "$RESOLVED_Q" ; then
  echo "[ERROR] バンドル対象 q のアーキテクチャが現在の環境 ($TARGET_ARCH) と一致しません: $RESOLVED_Q" >&2
  file -b "$RESOLVED_Q" || true
  exit 1
fi

echo "[INFO] q: $RESOLVED_Q (arch: $TARGET_ARCH)"

# venv 準備
if [ -z "${VIRTUAL_ENV:-}" ]; then
  echo "[INFO] venv を自動作成/使用します"
  python3 -m venv "$ROOT_DIR/venv"
  source "$ROOT_DIR/venv/bin/activate"
fi

pip install --upgrade pip wheel
pip install -r "$ROOT_DIR/requirements.txt"

# 旧ビルド成果物の掃除
rm -rf "$ROOT_DIR/dist/${APP_NAME}" "$ROOT_DIR/dist/${APP_NAME}.app" "$ROOT_DIR/build/${APP_NAME}" || true

# PyInstaller オプション
PYI_OPTS=(
  --windowed
  --noconfirm
  --clean
  --add-binary "${RESOLVED_Q}:."
)

# .icns を用意してアイコンに設定
_ensure_icns || true
if [ -n "$ICON_PATH" ] && [ -f "$ICON_PATH" ]; then
  PYI_OPTS+=( --icon "$ICON_PATH" )
  echo "[INFO] アイコン設定: $ICON_PATH"
else
  echo "[INFO] アイコン未設定（.icns が見つからないため）"
fi

# ビルド実行
cd "$ROOT_DIR"
streamlit-desktop-app build "$ENTRY" --name "$APP_NAME" --pyinstaller-options "${PYI_OPTS[@]}"

APP_PATH1="${ROOT_DIR}/dist/${APP_NAME}/${APP_NAME}.app"
APP_PATH2="${ROOT_DIR}/dist/${APP_NAME}.app"
if [ -d "$APP_PATH1" ]; then
  echo "[OK] ビルド完了: $APP_PATH1"
elif [ -d "$APP_PATH2" ]; then
  echo "[OK] ビルド完了: $APP_PATH2"
else
  echo "[ERROR] .app の生成を確認できませんでした (dist 内容を表示)" >&2
  ls -la "$ROOT_DIR/dist" || true
  exit 1
fi
