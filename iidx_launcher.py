"""IIDX launcher: separate analyzer process, integrated output, independent tools."""
import os
import ctypes
from ctypes import wintypes
import re
import sys
import queue
import signal
import subprocess
import shutil
import threading
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import ttk, messagebox

from iidx_paths import app_root
BASE = app_root()
MAIN = BASE / 'iidx_main.py'
TOOLS = (
    ('検出帯設定', BASE / 'Tool' / 'iidx_detection_band_settings.py'),
    ('履歴オーバーレイ設定', BASE / 'Tool' / 'iidx_history_overlay_settings.py'),
    ('RANDOM分析結果表示設定', BASE / 'Tool' / 'iidx_random_overlay_settings.py'),
    ('曲名DB編集', BASE / 'data' / 'iidx_song_title_editor.py'),
)
MAX_LINES = 3000
# Entry-point mode routing is opt-in for Python testing and automatic when frozen.
ENTRY = BASE / 'iidx_entry.py'
MODE_BY_TOOL = {
    'iidx_detection_band_settings.py': 'detection-band',
    'iidx_history_overlay_settings.py': 'history-settings',
    'iidx_random_overlay_settings.py': 'random-settings',
    'iidx_song_title_editor.py': 'song-editor',
}


def entry_mode_enabled():
    return getattr(sys, 'frozen', False) or os.environ.get('IIDX_USE_SINGLE_ENTRY') == '1'


def entry_command(mode, *args):
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--mode', mode, *args]
    return [python_executable(), '-u', str(ENTRY), '--mode', mode, *args]



def python_executable():
    exe = Path(sys.executable)
    if sys.platform == 'win32' and exe.name.lower() == 'pythonw.exe':
        candidate = exe.with_name('python.exe')
        if candidate.is_file():
            return str(candidate)
    return str(exe)


def popen_flags(group=False):
    if sys.platform != 'win32':
        return 0
    flags = subprocess.CREATE_NO_WINDOW
    if group:
        flags |= subprocess.CREATE_NEW_PROCESS_GROUP
    return flags


# Match iidx_main.py's Tesseract search order exactly.
TESSERACT_EXE = Path(r'C:\Program Files\Tesseract-OCR\tesseract.exe')


def check_tesseract():
    exe = str(TESSERACT_EXE) if TESSERACT_EXE.is_file() else shutil.which('tesseract')
    if not exe:
        return False, ('Tesseract OCR が見つかりません。\n'
                       'Tesseract OCR をインストールし、標準のインストール先に配置するか、\n'
                       'tesseract.exe の場所を Windows の PATH に登録してください。')
    try:
        result = subprocess.run(
            [exe, '--list-langs'], capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=10,
            creationflags=popen_flags())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f'Tesseract OCR の実行確認に失敗しました。\n{exc}'
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        return False, f'Tesseract OCR が正常終了しませんでした。\n{details[:600]}'
    # --list-langs outputs one language code per line after a heading.
    langs = {line.strip() for line in result.stdout.splitlines()}
    missing = sorted({'jpn', 'eng'} - langs)
    if missing:
        return False, ('Tesseract OCR の言語データが不足しています：' + ', '.join(missing)
                       + '\n日本語（jpn）と英語（eng）の両方をインストールしてください。')
    return True, f'Tesseract OCR：正常（jpn / eng）  [{exe}]'


class Launcher:
    def __init__(self, root):
        self.root = root
        root.title('IIDX RANDOM分析 - 総合メニュー')
        root.geometry('920x590')
        self.proc = None
        self.stopping = False
        self.tesseract_ready = False
        self.tesseract_check_id = 0
        self.tesseract_checking = False
        self.events = queue.Queue()
        self.lines = deque(maxlen=MAX_LINES)
        self.tools = []
        self.debug = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value='停止中')
        self.song = tk.StringVar(value='曲名：—')
        self.normal = tk.StringVar(value='正規譜面：—')
        self.random = tk.StringVar(value='RANDOM分析結果：—')

        outer = ttk.Frame(root, padding=14)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='IIDX RANDOM分析', font=('', 18, 'bold')).pack(anchor='w')
        bar = ttk.Frame(outer)
        bar.pack(fill='x', pady=(12, 10))
        self.start_button = ttk.Button(bar, text='▶ スタート', command=self.start, state='disabled')
        self.start_button.pack(side='left', padx=(0, 8))
        self.stop_button = ttk.Button(bar, text='■ ストップ', command=self.stop, state='disabled')
        self.stop_button.pack(side='left', padx=(0, 8))
        ttk.Button(bar, text='⚙ 設定', command=self.open_settings).pack(side='left')
        self.recheck_button = ttk.Button(bar, text='Tesseract 再チェック', command=self.begin_tesseract_check)
        self.recheck_button.pack(side='left', padx=(8, 0))
        ttk.Label(bar, textvariable=self.status).pack(side='right')
        self.tesseract_status = tk.StringVar(value='Tesseract OCR：確認中…')
        ttk.Label(outer, textvariable=self.tesseract_status, wraplength=860).pack(anchor='w', pady=(0, 8))
        info = ttk.LabelFrame(outer, text='分析情報', padding=10)
        info.pack(fill='x', pady=(0, 12))
        for var in (self.song, self.normal, self.random):
            ttk.Label(info, textvariable=var, wraplength=820).pack(anchor='w', pady=3)
        ttk.Checkbutton(outer, text='詳細ログを表示（デバッグ用）', variable=self.debug,
                        command=self.toggle_debug).pack(anchor='w', pady=(0, 6))
        self.log_frame = ttk.Frame(outer)
        self.log = tk.Text(self.log_frame, state='disabled', wrap='none', height=15,
                           font=('Consolas', 10))
        scroll = ttk.Scrollbar(self.log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.hotkey_thread_id = None
        self.hotkey_thread = None
        self.register_hotkeys()
        self.root.after(80, self.poll)
        self.begin_tesseract_check()

    def begin_tesseract_check(self):
        if self.tesseract_checking:
            return
        self.tesseract_checking = True
        self.tesseract_ready = False
        self.tesseract_check_id += 1
        check_id = self.tesseract_check_id
        self.start_button.configure(state='disabled')
        self.recheck_button.configure(state='disabled')
        self.tesseract_status.set('Tesseract OCR：確認中…')
        threading.Thread(target=self._tesseract_check_worker, args=(check_id,), daemon=True).start()

    def _tesseract_check_worker(self, check_id):
        try:
            ok, detail = check_tesseract()
        except Exception as exc:
            ok, detail = False, f'Tesseract OCR の確認で予期しないエラーが発生しました：\n{exc}'
        self.events.put(('tesseract_check', check_id, ok, detail))

    def register_hotkeys(self):
        """Use a dedicated Windows message loop; Tk consumes messages on its own thread."""
        if sys.platform != 'win32':
            return
        self.hotkey_thread = threading.Thread(target=self._hotkey_worker, daemon=True)
        self.hotkey_thread.start()

    def _hotkey_worker(self):
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.UnregisterHotKey.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
        user32.GetMessageW.restype = ctypes.c_int
        user32.PeekMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT)
        user32.PeekMessageW.restype = wintypes.BOOL
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        msg = wintypes.MSG()
        # Ensure this worker owns a message queue before the GUI can request shutdown.
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        self.hotkey_thread_id = kernel32.GetCurrentThreadId()
        registered = []
        try:
            for hotkey_id, letter in ((101, 'Q'), (102, 'W')):
                # MOD_ALT | MOD_NOREPEAT
                if user32.RegisterHotKey(None, hotkey_id, 0x4001, ord(letter)):
                    registered.append(hotkey_id)
                    self.events.put(('hotkey_status', f'Alt + {letter}: 登録成功'))
                else:
                    error = ctypes.get_last_error()
                    self.events.put(('hotkey_error', f'Alt + {letter} の登録に失敗しました（Windowsエラー {error}）。'))
            while True:
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result <= 0:  # WM_QUIT or error
                    break
                if msg.message == 0x0312:  # WM_HOTKEY
                    if msg.wParam == 101:
                        self.events.put(('hotkey', 'start'))
                    elif msg.wParam == 102:
                        self.events.put(('hotkey', 'stop'))
        finally:
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)
            self.hotkey_thread_id = None

    def toggle_debug(self):
        if self.debug.get():
            self.log_frame.pack(fill='both', expand=True)
            self.log.configure(state='normal')
            self.log.delete('1.0', 'end')
            self.log.insert('end', '\n'.join(self.lines) + ('\n' if self.lines else ''))
            self.log.see('end')
            self.log.configure(state='disabled')
        else:
            self.log_frame.pack_forget()

    def start(self):
        if not self.tesseract_ready:
            return
        if self.proc and self.proc.poll() is None:
            return
        use_entry = entry_mode_enabled()
        if not use_entry and not MAIN.is_file():
            messagebox.showerror('起動できません', f'メインプログラムがありません：\n{MAIN}')
            return
        if use_entry and not getattr(sys, 'frozen', False) and not ENTRY.is_file():
            messagebox.showerror('起動できません', f'エントリーポイントがありません：\n{ENTRY}')
            return
        cmd = (entry_command('analyzer', '--launcher-mode') if use_entry
               else [python_executable(), '-u', str(MAIN), '--launcher-mode'])
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=str(BASE),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE, bufsize=0,
                creationflags=popen_flags(group=True),
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        except OSError as exc:
            messagebox.showerror('起動失敗', str(exc))
            return
        self.stopping = False
        self.status.set('分析中')
        self.start_button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self.song.set('曲名：検出待機中')
        self.normal.set('正規譜面：—')
        self.random.set('RANDOM分析結果：—')
        self.lines.clear()
        self.toggle_debug()
        self.events.put(('line', '[LAUNCHER] 分析プロセスを起動しました'))
        threading.Thread(target=self.read_output, args=(self.proc,), daemon=True).start()

    def read_output(self, proc):
        try:
            # Binary pipe + explicit UTF-8 decoding avoids locale-dependent encoding.
            for raw in iter(proc.stdout.readline, b''):
                self.events.put(('line', raw.decode('utf-8', errors='replace').rstrip('\r\n')))
        except (OSError, ValueError) as exc:
            self.events.put(('line', f'[LAUNCHER] ログ取得エラー: {exc}'))
        finally:
            if proc.stdout:
                proc.stdout.close()
            code = proc.wait()
            self.events.put(('exit', proc, code))

    def handle_line(self, line):
        self.lines.append(line)
        if self.debug.get():
            self.log.configure(state='normal')
            self.log.insert('end', line + '\n')
            if int(self.log.index('end-1c').split('.')[0]) > MAX_LINES + 100:
                self.log.delete('1.0', '101.0')
            self.log.see('end')
            self.log.configure(state='disabled')

        # The analyzer emits structured English tags; keep them unchanged.
        # Only translate and extract their values for the launcher display.
        if '[PRE_PLAY DETECTED]' in line or '[TITLE OCR START]' in line:
            self.song.set('曲名：検出中')
            self.normal.set('正規譜面：—')
            self.random.set('RANDOM分析結果：—')
        if '[SONG IDENTIFIED]' in line:
            match = re.search(r"\btitle=(['\"])(.*?)\1", line)
            if match:
                self.song.set('曲名：' + match.group(2))
        if '[CHART FOUND]' in line or '[NORMAL RECORD SKIPPED]' in line:
            self.normal.set('正規譜面：取得済み')
        elif '[NORMAL RECORD START]' in line:
            self.normal.set('正規譜面：取得中')
        elif '[NORMAL CHART REGISTERED]' in line:
            self.normal.set('正規譜面：取得完了')
        elif '[CHART NOT REGISTERED]' in line:
            self.normal.set('正規譜面：未取得')
        elif '[NORMAL CHART SAVE ERROR]' in line:
            self.normal.set('正規譜面：保存失敗')
        elif '[NORMAL RECORD CANCELLED]' in line:
            self.normal.set('正規譜面：取得中断')
        elif '[CHART LEVEL MISMATCH]' in line:
            self.normal.set('正規譜面：レベル不一致')

        if '[RANDOM ANALYSIS START]' in line:
            self.random.set('RANDOM分析結果：分析中')
        elif '[DISPLAY UPDATED]' in line:
            match = re.search(r'\blayout=([1-7]{7})(?![0-9])', line)
            if match:
                self.random.set('RANDOM分析結果：' + match.group(1))
        elif '[RANDOM ANALYSIS STOP]' in line and 'FAILED' in line:
            self.random.set('RANDOM分析結果：確定失敗')
        if '[RANDOM SETUP ERROR]' in line or '[TEMPLATE LOAD FAILED]' in line:
            self.status.set('設定エラー（詳細ログを確認）')

    def poll(self):
        for _ in range(200):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == 'tesseract_check':
                _, check_id, ok, detail = event
                if check_id != self.tesseract_check_id:
                    continue
                self.tesseract_checking = False
                self.tesseract_ready = ok
                self.recheck_button.configure(state='normal')
                self.tesseract_status.set(detail if ok else 'Tesseract OCR：利用不可（再チェック可能）')
                if ok:
                    if self.proc is None or self.proc.poll() is not None:
                        self.start_button.configure(state='normal')
                    self.handle_line('[TESSERACT CHECK] OK: ' + detail)
                else:
                    self.start_button.configure(state='disabled')
                    self.handle_line('[TESSERACT CHECK] FAILED: ' + detail.replace('\n', ' / '))
                    messagebox.showerror('Tesseract OCR の確認', detail)
            elif event[0] == 'hotkey':
                if event[1] == 'start':
                    self.start()
                elif event[1] == 'stop':
                    self.stop(confirm=False)
            elif event[0] == 'hotkey_error':
                messagebox.showwarning('ホットキー登録失敗', event[1])
            elif event[0] == 'hotkey_status':
                self.handle_line('[HOTKEY] ' + event[1])
            elif event[0] == 'line':
                self.handle_line(event[1])
            elif event[0] == 'exit':
                _, proc, code = event
                if self.proc is proc:
                    self.proc = None
                    self.stopping = False
                    self.start_button.configure(state='normal' if self.tesseract_ready else 'disabled')
                    self.stop_button.configure(state='disabled')
                    self.status.set(f'停止中（終了コード: {code}）')
                    self.handle_line(f'[LAUNCHER] 分析終了 code={code}')
        self.root.after(80, self.poll)

    def stop(self, confirm=True):
        proc = self.proc
        if proc is None or proc.poll() is not None or self.stopping:
            return
        if confirm and not messagebox.askyesno('分析停止', '分析プロセスを停止しますか？\n処理中のデータがある場合は失われる可能性があります。'):
            return
        self.stopping = True
        self.status.set('停止処理中')
        self.stop_button.configure(state='disabled')
        threading.Thread(target=self._stop_worker, args=(proc,), daemon=True).start()

    def _stop_worker(self, proc):
        try:
            if proc.stdin:
                proc.stdin.write(b'STOP\n')
                proc.stdin.flush()
            try:
                proc.wait(timeout=10)
                return
            except subprocess.TimeoutExpired:
                self.events.put(('line', '[LAUNCHER] 正常終了待機がタイムアウトしたため強制停止します'))
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            self.events.put(('line', f'[LAUNCHER] 停止エラー: {exc}'))

    def open_settings(self):
        win = tk.Toplevel(self.root)
        win.title('IIDX 設定ランチャー')
        win.geometry('440x370')
        win.transient(self.root)
        frame = ttk.Frame(win, padding=20)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='設定ツール', font=('', 14, 'bold')).pack(anchor='w', pady=(0, 14))
        for label, path in TOOLS:
            ttk.Button(frame, text=label, command=lambda p=path: self.launch_tool(p)).pack(fill='x', pady=5)

        # Spout sender name: selectable/copyable, but not editable.
        spout_frame = ttk.LabelFrame(frame, text='Spout接続名', padding=(10, 8))
        spout_frame.pack(fill='x', pady=(12, 0))
        spout_name = 'Spout_OBS_Filter'
        spout_entry = ttk.Entry(spout_frame)
        spout_entry.insert(0, spout_name)
        spout_entry.configure(state='readonly')
        spout_entry.pack(side='left', fill='x', expand=True, padx=(0, 8))
        ttk.Button(spout_frame, text='コピー',
                   command=lambda: self.copy_spout_name(spout_name)).pack(side='right')

    def copy_spout_name(self, name):
        self.root.clipboard_clear()
        self.root.clipboard_append(name)
        self.root.update_idletasks()


    def launch_tool(self, path):
        use_entry = entry_mode_enabled()
        if not use_entry and not path.is_file():
            messagebox.showerror('ツールがありません', f'次の場所に配置してください：\n{path}')
            return
        if use_entry:
            mode = MODE_BY_TOOL.get(path.name)
            if mode is None:
                messagebox.showerror('起動できません', f'不明な設定ツール：\n{path}')
                return
            try:
                proc = subprocess.Popen(entry_command(mode), cwd=str(BASE),
                                        creationflags=popen_flags())
                self.tools.append(proc)
            except OSError as exc:
                messagebox.showerror('ツール起動失敗', str(exc))
            return
        try:
            exe = python_executable()
            if sys.platform == 'win32':
                candidate = Path(exe).with_name('pythonw.exe')
                if candidate.is_file():
                    exe = str(candidate)
            proc = subprocess.Popen([exe, str(path)], cwd=str(BASE),
                                    creationflags=popen_flags())
            self.tools.append(proc)
        except OSError as exc:
            messagebox.showerror('ツール起動失敗', str(exc))

    def close(self):
        if self.proc and self.proc.poll() is None:
            messagebox.showwarning('分析実行中', '先に「ストップ」で分析を終了してください。')
            return
        if sys.platform == 'win32' and self.hotkey_thread_id is not None:
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
            user32.PostThreadMessageW.restype = wintypes.BOOL
            user32.PostThreadMessageW(self.hotkey_thread_id, 0x0012, 0, 0)  # WM_QUIT
        if self.hotkey_thread is not None:
            self.hotkey_thread.join(timeout=1.0)
        self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    Launcher(root)
    root.mainloop()
