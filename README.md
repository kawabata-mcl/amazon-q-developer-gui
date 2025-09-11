# Amazon Q Developer GUI (Streamlit)

## 目的
配布先に Amazon Q Developer CLI (q) が未導入でも、そのまま起動できる macOS .app を提供します。本プロジェクトは q をアプリに同梱して配布します。

## 前提
- ビルドは配布対象と同じアーキテクチャの macOS 上で行ってください（Intel/Apple Silicon 一致）。
- 本格配布時はコード署名・公証を行ってください。

## ビルド手順（q を必ず同梱）

q の解決順は以下です。いずれか1つを用意してください。
- 環境変数 `Q_BINARY`: 同梱したい q のフルパス
- `vendor/q`: リポジトリ内に q 実行ファイルを配置（実行権限付与）
- 環境変数 `Q_BINARY_URL`: ビルド時に q をダウンロード（.dmg / .zip / 生バイナリ対応）

代表的なURL例（参考）
- macOS DMG（GUIインストーラ）: `https://desktop-release.q.us-east-1.amazonaws.com/latest/Amazon%20Q.dmg`
- Linux Debian (.deb): `https://desktop-release.q.us-east-1.amazonaws.com/latest/amazon-q.deb`
- Linux AppImage: `https://desktop-release.q.us-east-1.amazonaws.com/latest/amazon-q.appimage`
- Linux ヘッドレス zip（例・要環境に応じて）: `https://desktop-release.q.us-east-1.amazonaws.com/latest/q-x86_64-linux.zip`

実行例:
```bash
cd /Users/kawabata/dev/amazon-q-developer-gui
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 1) Q_BINARY を使う
Q_BINARY=/full/path/to/q ./build-macos.sh

# 2) vendor/q を使う（事前に配置・chmod +x vendor/q）
./build-macos.sh

# 3) Q_BINARY_URL を使う（macOSはDMG推奨）
Q_BINARY_URL="https://desktop-release.q.us-east-1.amazonaws.com/latest/Amazon%20Q.dmg" ./build-macos.sh
```

- 成果物: `dist/AmazonQDesktop.app`（または `dist/AmazonQDesktop/AmazonQDesktop.app`）
- アプリ内では同梱 q を優先して使用します。

## DMG 作成（配布用）
ビルド後、配布用の DMG を生成できます。
```bash
./make-dmg.sh
# 生成物: AmazonQDesktop-Installer.dmg
```

### DMG の見た目・アイコンについて
- アプリのアイコンは `images/AmazonQDeveloperGui.png` から自動的に `.icns` を生成し、.app に適用されます（macOS の `sips` と `iconutil` コマンドが必要）。
- DMG のウィンドウは、左側にアプリ、右側に `Applications` エイリアスが配置され、ドラッグ＆ドロップによるインストールが分かりやすい表示になります。
- DMG の背景画像をカスタマイズしたい場合は、`images/dmg-background.png` を用意してください。存在する場合に自動的に適用されます。

### インストール方法（エンドユーザー向け）
1. 生成された `AmazonQDesktop-Installer.dmg` を開きます。
2. 表示されたウィンドウで、`AmazonQDesktop.app` を右側の `Applications` フォルダへドラッグ＆ドロップします。
3. コピー完了後、`/Applications` から `AmazonQDesktop` を起動してください。

## ローカル開発（任意）
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## 注意事項
- 初回起動時にファイアウォール確認ダイアログが表示される場合があります（ローカルサーバ利用のため）。
- 署名・公証をしない場合、Gatekeeper 警告が表示されます。
