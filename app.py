import os
import re
import time
import shutil
import subprocess
from typing import List, Optional

import streamlit as st
import threading
import queue
from datetime import datetime

# ===== Debug settings =====
# コード上の変数で切り替え可能なデバッグモード（UIからは変更しない）
DEBUG_MODE: bool = True  # True にすると詳細ログを出力
DEBUG_LOG_DIR: str = os.path.join(os.path.expanduser("."), "logs")

# プロンプトは環境により 'Amazon Q>' または '>' の場合がある（行全体がプロンプトで終わる想定）
PROMPT_REGEX = re.compile(r"(?m)^\s*(?:Amazon Q\>|\>)\s*$")
ANSI_ESCAPE = re.compile(r"\x1B\[[0-9;?]*[ -/]*[@-~]")
# Startup messages we may need to handle to get to the prompt quickly
INIT_CTRL_C = re.compile(r"ctrl.?\+?c to start chatting", re.IGNORECASE)
LEGACY_PROMPT = re.compile(r"Legacy profiles detected.*migrate", re.IGNORECASE)
YNT_PROMPT_RE = re.compile(r"(?i)\[\s*y\s*/\s*n(?:\s*/\s*t)?\s*\]:")
ALLOW_ACTION_PROMPT_RE = re.compile(r"(?i)Allow\s+this\s+action\?.*Use\s*'t'\s*to\s*trust")

def _strip_ansi_all(text: str) -> str:
    """Strip common ANSI/terminal control sequences including CSI, OSC, and ESC7/ESC8."""
    if not text:
        return text
    # Remove CSI sequences (ESC [ ...)
    s = ANSI_ESCAPE.sub("", text)
    # Remove Operating System Command sequences: ESC ] ... (terminated by BEL or ST)
    s = re.sub(r"\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)", "", s)
    # Remove ESC 7 (save cursor) and ESC 8 (restore cursor)
    s = s.replace("\x1B7", "").replace("\x1B8", "")
    return s

def _filter_transient_status(text: str) -> str:
    """一時的なステータス（Thinking スピナー等）を非表示にする。
    以下のような出力を削除対象とする：
    - "Thinking..." / "Thinking…" のみ、またはそれが繰り返された行
    - 単一文字の断片（t h i n k g など）に句読点のみが付いた行
    - 先頭の箇条書き記号（•, -, * , ·）や空白を許容
    実テキストはストリーム断片で渡ってくる可能性があるため、行単位で判定する。
    """
    if not text:
        return text
    # まずは行内に埋め込まれたスピナー + Thinking トークン列を一括除去
    # 例: "⠋ Thinking... ⠙ Thinking... ⠹ Thinking... > AWS..."
    p_inline_seq = re.compile(r"(?i)(?:[\u2800-\u28FF•]*\s*Thinking(?:\s*(?:\.{3}|…|[.!?]))+\s*)+>?\s*")
    text = p_inline_seq.sub("", text)

    lines = text.splitlines(keepends=True)
    out_lines = []
    # パターン: Thinking... がスペース・句読点で区切られて繰り返されるだけの行
    p_thinking = re.compile(r"^(?:[ \t•\-\*·]*Thinking(?:[ \t]*[\.…!]+)?[ \t]*)+$", re.IGNORECASE)
    # パターン: thinking の文字断片のみ（1〜10 文字）に句読点が付いただけの行
    p_frag = re.compile(r"^[ \t•\-\*·]*[tThHiInNkKgG]{1,10}[ \t\.·…!]*$")
    # パターン: ブロック点字(⠋など)のみのスピナー行
    p_braille = re.compile(r"^[ \t\u2800-\u28FF•·\-\*\.…!>]+$")
    for ln in lines:
        core = _strip_ansi_all(ln).strip()
        if not core:
            out_lines.append(ln)
            continue
        if p_thinking.match(core):
            continue
        if p_frag.match(core):
            continue
        if p_braille.match(core):
            continue
        out_lines.append(ln)
    cleaned = "".join(out_lines)
    # 連続改行を圧縮
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned

def _remove_input_echo_once(text: str, user_text: str) -> str:
    """最初のチャンクに含まれる可能性がある入力エコーを一度だけ取り除く。
    フィルタは最小限（他の文言は非表示にしない）。
    """
    if not text or not user_text:
        return text
    candidates = [
        user_text,
        user_text + "\n",
        user_text + "\r\n",
        f"Amazon Q> {user_text}\n",
        f"Amazon Q> {user_text}\r\n",
        f"> {user_text}\n",
        f"> {user_text}\r\n",
    ]
    for c in candidates:
        idx = text.find(c)
        if idx != -1:
            return text.replace(c, "", 1)
    return text

class QChatSession:
    def __init__(self, trust_fs_write: bool = False, trust_execute_bash: bool = False, q_log_level: str = "info", cwd: Optional[str] = None, debug: bool = DEBUG_MODE):
        self.trust_fs_write = trust_fs_write
        self.trust_execute_bash = trust_execute_bash
        self.q_log_level = q_log_level
        # 実行ディレクトリ（未指定時は ~/amazon-q を利用）
        self.cwd: str = cwd or os.path.join(os.path.expanduser("~"), "amazon-q")
        self.proc: Optional[subprocess.Popen] = None
        self._q: Optional[queue.Queue] = None  # 出力統合キュー（stdout/stderr）
        self._threads: List[threading.Thread] = []
        # Debug
        self.debug: bool = bool(debug)
        self.log_dir: str = DEBUG_LOG_DIR
        self.log_file_path: Optional[str] = None
        self._log_fp = None

    def _log(self, msg: str) -> None:
        if not self.debug:
            return
        try:
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            if self._log_fp:
                self._log_fp.write(f"[{ts}] {msg}\n")
                self._log_fp.flush()
        except Exception:
            pass

    def start(self) -> str:
        args: List[str] = ["chat"]
        trusted_tools = ["fs_read"]
        if self.trust_fs_write:
            trusted_tools.append("fs_write")
        if self.trust_execute_bash:
            trusted_tools.append("execute_bash")
        if trusted_tools:
            args.append(f"--trust-tools={','.join(trusted_tools)}")

        env = os.environ.copy()
        if self.q_log_level:
            env["Q_LOG_LEVEL"] = self.q_log_level
        # Ensure interactive-friendly environment
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("LANG", "C.UTF-8")
        env.setdefault("LC_ALL", "C.UTF-8")

        # Debug: log pathとlogfileの準備
        if self.debug:
            try:
                os.makedirs(self.log_dir, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                self.log_file_path = os.path.join(self.log_dir, f"qchat_{ts}.log")
                # line-buffered text file
                self._log_fp = open(self.log_file_path, "a", encoding="utf-8", buffering=1)
            except Exception:
                self._log_fp = None

        # Spawn q chat as a background process (no pseudo-TTY)
        os.makedirs(self.cwd, exist_ok=True)
        cmd = ["q"] + args
        self.proc = subprocess.Popen(
            cmd,
            cwd=self.cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
        )

        self._q = queue.Queue()

        def _reader(stream):
            try:
                while True:
                    chunk = stream.readline()
                    if chunk == "":
                        break
                    # ログへそのまま書き出し（任意）
                    if self._log_fp:
                        try:
                            self._log_fp.write(chunk)
                        except Exception:
                            pass
                    self._q.put(chunk)
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        # Start reader thread (stdout only; stderr already merged)
        t_out = threading.Thread(target=_reader, args=(self.proc.stdout,), daemon=True)
        t_out.start()
        self._threads = [t_out]
        self._log(f"SPAWN: cmd='{' '.join(cmd)}' cwd='{self.cwd}'")
        self._log(f"ENV: Q_LOG_LEVEL={env.get('Q_LOG_LEVEL')} TERM={env.get('TERM')} LANG={env.get('LANG')} LC_ALL={env.get('LC_ALL')}")

        # Warm up: gather initial output until we detect prompt or timeout
        initial_output = ""
        saw_prompt = False
        last_any_ts = time.time()
        silence_after_prompt_sec = 0.7
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                chunk = self._q.get(timeout=0.5)
                initial_output += chunk
                last_any_ts = time.time()
                cleaned = _strip_ansi_all(initial_output)
                if not saw_prompt and PROMPT_REGEX.search(cleaned):
                    saw_prompt = True
            except queue.Empty:
                if saw_prompt and (time.time() - last_any_ts > silence_after_prompt_sec):
                    break
                continue

        cleaned_full = _strip_ansi_all(initial_output)
        self._log("STATE: ready (prompt reached)")
        return cleaned_full

    def close(self) -> None:
        if self.proc is not None:
            try:
                if self.proc.stdin:
                    try:
                        self.proc.stdin.write("/quit\n")
                        self.proc.stdin.flush()
                    except Exception:
                        pass
                # Give it a moment to exit gracefully
                try:
                    self.proc.wait(timeout=2)
                except Exception:
                    pass
                if self.proc.poll() is None:
                    try:
                        self.proc.terminate()
                    except Exception:
                        pass
                    try:
                        self.proc.wait(timeout=2)
                    except Exception:
                        pass
                if self.proc.poll() is None:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
            finally:
                self.proc = None
        if self._log_fp:
            try:
                self._log_fp.close()
            except Exception:
                pass
            self._log_fp = None

    def send_and_stream(self, text: str):
        if not self.proc or not self._q:
            raise RuntimeError("Chat session not started")

        # Drain any stale output before sending a new message
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

        # Send user message
        try:
            if self.proc.stdin:
                self.proc.stdin.write(text + "\n")
                self.proc.stdin.flush()
        except Exception as e:
            yield f"\n[Error writing to q chat stdin: {e}]\n"
            return
        self._log(f"SEND: '{text[:80] + ('…' if len(text) > 80 else '')}'")

        # Stream response by incrementally reading and detecting the prompt in the cleaned buffer
        raw_buf = ""
        cleaned_len_emitted = 0
        saw_output = False  # 意味のある本文を一度でも出したか
        last_output_ts = time.time()
        last_any_ts = time.time()  # 何らかの出力が来た時刻
        deadline = time.time() + 60  # ターンの最大待機時間（秒）
        kick_sent = False  # 無反応時の空行送信フラグ
        first_emit = True

        while True:
            try:
                chunk = self._q.get(timeout=1)
                raw_buf += chunk
                last_any_ts = time.time()
                cleaned = _strip_ansi_all(raw_buf)
                m = PROMPT_REGEX.search(cleaned)
                end_idx = m.start() if m else len(cleaned)
                if end_idx > cleaned_len_emitted:
                    to_emit = cleaned[cleaned_len_emitted:end_idx]
                    if to_emit:
                        if first_emit:
                            to_emit = _remove_input_echo_once(to_emit, text)
                            first_emit = False
                        # まず 'Allow this action?... Use 't' to trust' を検出
                        allow_match = ALLOW_ACTION_PROMPT_RE.search(to_emit)
                        if allow_match:
                            before = to_emit[:allow_match.start()]
                            before = _filter_transient_status(before)
                            if before:
                                yield before
                            yield {"type": "permission", "prompt": to_emit[allow_match.start():].strip()}
                            return
                        # 権限確認プロンプトの検出（[y/n/t]:）
                        perm_match = YNT_PROMPT_RE.search(to_emit)
                        if perm_match:
                            before = to_emit[:perm_match.start()]
                            before = _filter_transient_status(before)
                            if before:
                                yield before
                            # UI にボタンを出すイベントを通知
                            yield {"type": "permission", "prompt": to_emit[perm_match.start():].strip()}
                            return
                        # 一時的なステータス（Thinking など）を除去
                        to_emit = _filter_transient_status(to_emit)
                        if to_emit:
                            yield to_emit
                            cleaned_len_emitted = end_idx
                            if to_emit.strip():
                                saw_output = True
                                last_output_ts = time.time()
                if m:
                    # いったんプロンプトを検出したら、わずかな沈黙を待ってターン終了
                    if time.time() - last_any_ts > 0.5:
                        break
            except queue.Empty:
                # no new data, keep waiting
                if saw_output and (time.time() - last_output_ts > 5.0):
                    break
                # 無反応が続く場合は一度だけ空行を送信して促す
                if (not saw_output) and (not kick_sent) and (time.time() - last_any_ts > 3.0):
                    try:
                        if self.proc.stdin:
                            self.proc.stdin.write("\n")
                            self.proc.stdin.flush()
                        kick_sent = True
                        self._log("KICK: sent extra newline due to silence >3s")
                    except Exception as e:
                        self._log(f"KICK: failed to send extra newline: {e}")
                if time.time() > deadline:
                    try:
                        alive = (self.proc.poll() is None)
                        self._log(f"TIMEOUT: 60s elapsed. proc.alive={alive}")
                    except Exception:
                        pass
                    break
                continue
            except Exception as e:
                yield f"\n[Error while reading output: {e}]\n"
                self._log(f"ERROR: send_and_stream exception: {e}")
                break

    def send_permission_choice(self, choice: str) -> None:
        """権限プロンプトに y/n/t を応答する。"""
        if not self.proc:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.write(choice.strip() + "\n")
                self.proc.stdin.flush()
                self._log(f"PERMISSION: sent '{choice}'")
        except Exception as e:
            self._log(f"PERMISSION: failed to send '{choice}': {e}")

    def stream_until_turn_end(self):
        """入力を送らず、現在のターンの残りをストリーミングする。
        権限プロンプトが出たら permission イベントを発火して戻る。
        """
        if not self.proc or not self._q:
            raise RuntimeError("Chat session not started")
        raw_buf = ""
        cleaned_len_emitted = 0
        saw_output = False
        last_output_ts = time.time()
        last_any_ts = time.time()
        deadline = time.time() + 60
        while True:
            try:
                chunk = self._q.get(timeout=1)
                raw_buf += chunk
                last_any_ts = time.time()
                cleaned = _strip_ansi_all(raw_buf)
                m = PROMPT_REGEX.search(cleaned)
                end_idx = m.start() if m else len(cleaned)
                if end_idx > cleaned_len_emitted:
                    to_emit = cleaned[cleaned_len_emitted:end_idx]
                    if to_emit:
                        # まず 'Allow this action?... Use 't' to trust' を検出
                        allow_match = ALLOW_ACTION_PROMPT_RE.search(to_emit)
                        if allow_match:
                            before = to_emit[:allow_match.start()]
                            before = _filter_transient_status(before)
                            if before:
                                yield before
                            yield {"type": "permission", "prompt": to_emit[allow_match.start():].strip()}
                            return
                        perm_match = YNT_PROMPT_RE.search(to_emit)
                        if perm_match:
                            before = to_emit[:perm_match.start()]
                            before = _filter_transient_status(before)
                            if before:
                                yield before
                            yield {"type": "permission", "prompt": to_emit[perm_match.start():].strip()}
                            return
                        to_emit = _filter_transient_status(to_emit)
                        if to_emit:
                            yield to_emit
                            cleaned_len_emitted = end_idx
                            if to_emit.strip():
                                saw_output = True
                                last_output_ts = time.time()
                if m:
                    if time.time() - last_any_ts > 0.5:
                        break
            except queue.Empty:
                if saw_output and (time.time() - last_output_ts > 5.0):
                    break
                if time.time() > deadline:
                    break
                continue
            except Exception as e:
                yield f"\n[Error while reading output: {e}]\n"
                break



def get_or_create_session(trust_fs_write: bool, trust_execute_bash: bool, q_log_level: str, cwd: str) -> QChatSession:
    sess: Optional[QChatSession] = st.session_state.get("qchat_session")
    if (
        sess is None
        or sess.trust_fs_write != trust_fs_write
        or sess.trust_execute_bash != trust_execute_bash
        or sess.q_log_level != q_log_level
        or getattr(sess, "cwd", None) != cwd
    ):
        if sess is not None:
            try:
                sess.close()
            except Exception:
                pass
        sess = QChatSession(
            trust_fs_write=trust_fs_write,
            trust_execute_bash=trust_execute_bash,
            q_log_level=q_log_level,
            cwd=cwd,
        )
        banner = sess.start()
        st.session_state["qchat_session"] = sess
        st.session_state.setdefault("messages", [])
        # 初期出力（MCP初期化や案内文など）はUIには表示しない
    return sess


def render_env_status() -> None:
    st.sidebar.subheader("環境状態")
    q_path = shutil.which("q")
    if not q_path:
        st.sidebar.error("q コマンドが見つかりません。Amazon Q Developer CLI をインストールしてください。")
        st.stop()
    else:
        st.sidebar.success(f"q: {q_path}")
        # Optional: show version and identity
        try:
            ver = subprocess.run(["q", "--version"], capture_output=True, text=True, timeout=5)
            who = subprocess.run(["q", "whoami"], capture_output=True, text=True, timeout=5)
            if ver.stdout:
                st.sidebar.caption(ver.stdout.strip())
            if who.stdout:
                st.sidebar.caption(who.stdout.strip())
        except Exception:
            pass
    # Debug info
    sess = st.session_state.get("qchat_session")
    if sess and getattr(sess, "debug", False):
        st.sidebar.warning("Debug mode 有効")
        if getattr(sess, "log_file_path", None):
            st.sidebar.caption(f"Log: {sess.log_file_path}")


def main():
    st.set_page_config(page_title="Amazon Q Chat (CLI)", page_icon="🤖", layout="wide")
    st.title("Amazon Q Developer CLI チャット (対話モード)")
    st.caption("Streamlit から `q chat` を対話セッションとして利用します。デフォルトは fs_read のみ信頼。必要に応じて fs_write / execute_bash を有効化できます。")

    # Sidebar controls
    render_env_status()

    st.sidebar.subheader("Trust 設定")
    opt_fs_write = st.sidebar.toggle("ファイル書き込みを許可 (fs_write)", value=False, help="ファイルの作成・変更を Q に許可します。")
    opt_execute_bash = st.sidebar.toggle("シェル実行を許可 (execute_bash)", value=False, help="外部コマンドの実行を Q に許可します。慎重に有効化してください。")

    st.sidebar.subheader("実行ディレクトリ")
    default_cwd = os.path.join(os.path.expanduser("~"), "amazon-q")
    cwd_input = st.sidebar.text_input(
        "作業ディレクトリ",
        value=st.session_state.get("cwd", default_cwd),
        help="既定は ~/amazon-q。存在しない場合は自動作成されます。",
    )
    st.session_state["cwd"] = cwd_input

    st.sidebar.subheader("Q 設定")
    q_log_level = st.sidebar.selectbox("Q_LOG_LEVEL", options=["error", "warn", "info", "debug", "trace"], index=2)

    # Session management (auto-recreate if options changed)
    sess = get_or_create_session(
        trust_fs_write=opt_fs_write,
        trust_execute_bash=opt_execute_bash,
        q_log_level=q_log_level,
        cwd=cwd_input,
    )

    if st.sidebar.button("セッション再起動"):
        try:
            sess.close()
        except Exception:
            pass
        del st.session_state["qchat_session"]
        st.rerun()

    # Display current trust summary
    trust_summary = ["fs_read"]
    if opt_fs_write:
        trust_summary.append("fs_write")
    if opt_execute_bash:
        trust_summary.append("execute_bash")
    st.info("現在の信頼ツール: " + ", ".join(trust_summary))
    st.caption(f"作業ディレクトリ: {cwd_input}")

    # Initialize chat history
    if "messages" not in st.session_state:
        st.session_state["messages"] = []

    # Render history（すべて表示）
    for m in st.session_state["messages"]:
        with st.chat_message(m["role"]):
            st.markdown(m["content"]) 

    # Permission pending UI
    pending = st.session_state.get("pending_permission")
    if pending:
        with st.chat_message("assistant"):
            placeholder = st.empty()
            collected = pending.get("collected", "")
            placeholder.markdown(collected)
            st.info("許可が必要な操作です。Amazon Q がツールを使用しようとしています。以下から選択してください。")
            if prompt := pending.get("prompt"):
                st.code(prompt)
            col1, col2, col3 = st.columns(3)
            yes = col1.button("はい（この操作を許可）", key="perm_yes")
            no = col2.button("いいえ（拒否）", key="perm_no")
            trust = col3.button("このセッションで常に許可（信頼）", key="perm_trust")

            def _continue_after_choice(choice: str):
                sess.send_permission_choice(choice)
                new_collected = collected
                encountered_permission = False
                for chunk in sess.stream_until_turn_end():
                    if isinstance(chunk, dict) and chunk.get("type") == "permission":
                        # 次の許可プロンプトに到達
                        st.session_state["pending_permission"] = {
                            "prompt": chunk.get("prompt", ""),
                            "collected": new_collected,
                        }
                        st.rerun()
                        return
                    else:
                        new_collected += chunk
                        placeholder.markdown(new_collected)
                # 追加の許可がなければメッセージを確定
                st.session_state["messages"].append({"role": "assistant", "content": new_collected.strip()})
                st.session_state["pending_permission"] = None
                st.rerun()

            if yes:
                _continue_after_choice("y")
            if no:
                _continue_after_choice("n")
            if trust:
                _continue_after_choice("t")

        # pending がある間は通常の入力を一時停止
        st.stop()

    # Single chat input（下部のネイティブ入力のみ）
    def process_message(message: str):
        st.session_state["messages"].append({"role": "user", "content": message})
        with st.chat_message("user"):
            st.markdown(message)
        with st.chat_message("assistant"):
            placeholder = st.empty()
            collected = ""
            for chunk in sess.send_and_stream(message):
                if isinstance(chunk, dict) and chunk.get("type") == "permission":
                    # 許可待ち状態に移行（この時点では確定させない）
                    st.session_state["pending_permission"] = {"prompt": chunk.get("prompt", ""), "collected": collected}
                    placeholder.markdown(collected)
                    st.rerun()
                    return
                else:
                    collected += chunk
                    placeholder.markdown(collected)
            # 応答の一部を保存（許可待ちが発生しなかった場合のみ）
            st.session_state["messages"].append({"role": "assistant", "content": collected.strip()})

    # チャット入力（シンプル）
    user_input = st.chat_input("Amazon Q にメッセージを送信")
    if user_input:
        process_message(user_input)


if __name__ == "__main__":
    main()
