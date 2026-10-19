"""IIDX SQLite song title editor. Standalone; does not import iidx_main."""
import sqlite3
import csv
import io
import json
import zipfile
import os
import tempfile
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from pathlib import Path
from datetime import datetime

import sys
APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from iidx_paths import app_root
BASE = app_root() / "data"
DB = BASE / 'iidx_songs.sqlite3'
BACKUP = BASE / 'db_backups'

class Editor:
    def __init__(self, root):
        self.root = root
        root.title('IIDX 曲名データベース編集')
        root.geometry('1000x630')
        self.conn = None
        self.selected_id = None
        self.original = None
        self.rows = []
        self.query = tk.StringVar()
        self.official = tk.StringVar()
        self.ocr = tk.StringVar()
        self.info = tk.StringVar(value='')
        top = ttk.Frame(root, padding=12)
        top.pack(fill='x')
        ttk.Label(top, text='曲名検索（正式曲名・OCR曲名・ID）').pack(anchor='w')
        ttk.Entry(top, textvariable=self.query).pack(fill='x', pady=(3, 7))
        self.query.trace_add('write', lambda *_: self.filter_rows())
        cols = ('id', 'official', 'ocr', 'charts')
        self.tree = ttk.Treeview(root, columns=cols, show='headings', selectmode='browse', height=13)
        for key, label, width in [('id', 'ID', 65), ('official', '正式曲名', 340), ('ocr', 'OCR照合用曲名', 340), ('charts', '譜面数', 70)]:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, stretch=key in ('official', 'ocr'))
        scroll = ttk.Scrollbar(root, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side='top', fill='both', expand=True, padx=(12, 30))
        self.tree.bind('<<TreeviewSelect>>', self.select_row)
        edit = ttk.LabelFrame(root, text='選択した曲を編集', padding=12)
        edit.pack(fill='x', padx=12, pady=8)
        ttk.Label(edit, text='正式曲名').grid(row=0, column=0, sticky='w', pady=5)
        ttk.Entry(edit, textvariable=self.official).grid(row=0, column=1, sticky='ew', padx=8, pady=5)
        ttk.Label(edit, text='OCR照合用曲名').grid(row=1, column=0, sticky='w', pady=5)
        ttk.Entry(edit, textvariable=self.ocr).grid(row=1, column=1, sticky='ew', padx=8, pady=5)
        edit.columnconfigure(1, weight=1)
        actions = ttk.Frame(root, padding=(12, 3, 12, 12))
        actions.pack(fill='x')
        ttk.Button(actions, text='変更を保存', command=self.save).pack(side='right', padx=4)
        ttk.Button(actions, text='選択曲を削除', command=self.delete_selected).pack(side='right', padx=12)
        ttk.Button(actions, text='選択曲を再読込', command=self.reload_selection).pack(side='right', padx=4)
        ttk.Button(actions, text='一覧を更新', command=self.load_rows).pack(side='left', padx=4)
        ttk.Button(actions, text='エクスポート', command=self.export_dialog).pack(side='left', padx=12)
        ttk.Button(actions, text='インポート', command=self.import_dialog).pack(side='left', padx=4)
        ttk.Label(root, textvariable=self.info, padding=(12, 0, 12, 10)).pack(anchor='w')
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.db_missing = not DB.is_file()
        try:
            if self.db_missing:
                # Work in memory until a validated import has completed.
                # Merely opening the GUI must not create an empty SQLite file.
                self.conn = sqlite3.connect(':memory:', timeout=3)
                self.conn.execute('PRAGMA foreign_keys=ON')
                self.conn.executescript("""
                    CREATE TABLE songs (
                        song_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        official_title TEXT NOT NULL UNIQUE,
                        ocr_title TEXT NOT NULL
                    );
                    CREATE TABLE normal_charts (
                        song_id INTEGER NOT NULL,
                        difficulty TEXT NOT NULL,
                        level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 12),
                        note_events TEXT NOT NULL,
                        PRIMARY KEY(song_id, difficulty),
                        FOREIGN KEY(song_id) REFERENCES songs(song_id)
                    );
                """)
                self.conn.commit()
            else:
                self.conn = sqlite3.connect(f'file:{DB.as_posix()}?mode=rw', uri=True, timeout=3)
                self.conn.execute('PRAGMA foreign_keys=ON')
                for table in ('songs', 'normal_charts'):
                    if self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
                        raise ValueError(f'テーブル {table} がありません')
                integrity = self.conn.execute('PRAGMA quick_check').fetchone()
                if integrity is None or integrity[0] != 'ok':
                    raise ValueError(f'データベース整合性エラー: {integrity}')
            self.load_rows()
            if self.db_missing:
                self.info.set(f'データベース未作成（0曲）。ZIPインポートで作成します / 保存先: {DB}')
        except (sqlite3.Error, ValueError) as exc:
            if self.conn is not None:
                self.conn.close()
                self.conn = None
            messagebox.showerror('DB読込エラー', str(exc))
            root.after(0, root.destroy)

    def load_rows(self):
        try:
            self.rows = self.conn.execute('''SELECT s.song_id, s.official_title, s.ocr_title, COUNT(c.song_id)
                FROM songs s LEFT JOIN normal_charts c ON c.song_id=s.song_id
                GROUP BY s.song_id ORDER BY s.song_id''').fetchall()
            self.filter_rows()
            self.info.set(f'登録曲数: {len(self.rows)} / DB: {DB}')
        except sqlite3.Error as exc:
            messagebox.showerror('読込エラー', str(exc))

    def filter_rows(self):
        needle = self.query.get().casefold().strip()
        selected = self.selected_id
        for item in self.tree.get_children():
            self.tree.delete(item)
        for row in self.rows:
            if needle and not any(needle in str(x).casefold() for x in row[:3]):
                continue
            self.tree.insert('', 'end', iid=str(row[0]), values=row)
        if selected is not None and self.tree.exists(str(selected)):
            self.tree.selection_set(str(selected))

    def select_row(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        new_id = int(selection[0])
        if self.selected_id != new_id and self.original is not None and (
            self.official.get(), self.ocr.get()) != self.original[1:]:
            if not messagebox.askyesno('未保存の変更', '未保存の編集があります。破棄して別の曲を選択しますか？'):
                if self.selected_id is not None and self.tree.exists(str(self.selected_id)):
                    self.tree.selection_set(str(self.selected_id))
                return
        self.selected_id = new_id
        self.reload_selection()

    def reload_selection(self):
        if self.selected_id is None:
            return
        try:
            row = self.conn.execute('SELECT song_id, official_title, ocr_title FROM songs WHERE song_id=?', (self.selected_id,)).fetchone()
            if row is None:
                raise ValueError('選択曲が削除されています。一覧を更新してください。')
            self.original = row
            self.official.set(row[1])
            self.ocr.set(row[2])
        except (sqlite3.Error, ValueError) as exc:
            messagebox.showerror('読込エラー', str(exc))

    def save(self):
        if self.original is None:
            messagebox.showinfo('未選択', '編集する曲を一覧から選択してください。')
            return
        song_id, old_official, old_ocr = self.original
        official, ocr = self.official.get().strip(), self.ocr.get().strip()
        if not official or not ocr:
            messagebox.showwarning('入力エラー', '正式曲名・OCR照合用曲名は空欄にできません。')
            return
        if (official, ocr) == (old_official, old_ocr):
            messagebox.showinfo('変更なし', '変更はありません。')
            return
        if not messagebox.askyesno('変更確認', f'ID {song_id} の曲名を変更します。\n\n正式曲名:\n{old_official}\n → {official}\n\nOCR照合用:\n{old_ocr}\n → {ocr}\n\n保存しますか？'):
            return
        try:
            # Backup using SQLite's online backup API before modifying any record.
            BACKUP.mkdir(exist_ok=True)
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            backup = BACKUP / f'iidx_songs_before_edit_{stamp}.sqlite3'
            with sqlite3.connect(str(backup)) as dst:
                self.conn.backup(dst)
            # Compare-and-swap prevents silently overwriting concurrent edits.
            cur = self.conn.execute('''UPDATE songs SET official_title=?, ocr_title=?
                WHERE song_id=? AND official_title=? AND ocr_title=?''',
                (official, ocr, song_id, old_official, old_ocr))
            if cur.rowcount != 1:
                self.conn.rollback()
                messagebox.showerror('更新競合', '別の処理で曲名が変更されています。再読込してください。')
                return
            self.conn.commit()
            self.original = (song_id, official, ocr)
            self.load_rows()
            if self.tree.exists(str(song_id)):
                self.tree.selection_set(str(song_id))
            messagebox.showinfo('保存完了', f'曲名を更新しました。\n\nバックアップ:\n{backup}')
        except (sqlite3.Error, OSError) as exc:
            self.conn.rollback()
            messagebox.showerror('保存エラー', f'保存できませんでした。\n{exc}')

    def delete_selected(self):
        """Delete a song and all its NORMAL charts, with a recoverable DB backup."""
        if self.selected_id is None or self.original is None:
            messagebox.showinfo('未選択', '削除する曲を一覧から選択してください。')
            return
        if self.db_missing:
            messagebox.showwarning('DB未作成', 'データベースがありません。')
            return
        if self.has_unsaved_changes():
            messagebox.showwarning('未保存の変更', '編集内容を保存または再読込してから削除してください。')
            return
        song_id = self.selected_id
        try:
            row = self.conn.execute('SELECT official_title, ocr_title FROM songs WHERE song_id=?', (song_id,)).fetchone()
            if row is None:
                messagebox.showwarning('削除対象なし', '曲が見つかりません。一覧を更新してください。')
                self.selected_id = None
                self.original = None
                self.load_rows()
                return
            count = self.conn.execute('SELECT COUNT(*) FROM normal_charts WHERE song_id=?', (song_id,)).fetchone()[0]
            if not messagebox.askyesno('曲と譜面の削除確認',
                    f'以下の曲と登録済みNORMAL譜面 {count}件を削除します。\n\n'
                    f'ID: {song_id}\n正式曲名: {row[0]}\n\n'
                    '削除前にDB全体のバックアップを作成します。\n'
                    '削除後、この曲は次回プレー時に再登録・再取得できます。\n\n実行しますか？',
                    icon='warning'):
                return
            BACKUP.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            backup = BACKUP / f'iidx_songs_before_delete_{stamp}.sqlite3'
            with sqlite3.connect(str(backup)) as dst:
                self.conn.backup(dst)
            try:
                self.conn.execute('BEGIN IMMEDIATE')
                current = self.conn.execute('SELECT official_title, ocr_title FROM songs WHERE song_id=?', (song_id,)).fetchone()
                if current != row:
                    raise ValueError('曲名が変更されています。再読込してから削除してください。')
                self.conn.execute('DELETE FROM normal_charts WHERE song_id=?', (song_id,))
                cur = self.conn.execute('DELETE FROM songs WHERE song_id=? AND official_title=? AND ocr_title=?',
                                        (song_id, row[0], row[1]))
                if cur.rowcount != 1:
                    raise ValueError('削除対象が変更されました。')
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            self.selected_id = None
            self.original = None
            self.official.set('')
            self.ocr.set('')
            self.load_rows()
            messagebox.showinfo('削除完了', f'曲ID {song_id} とNORMAL譜面 {count}件を削除しました。\n\nバックアップ:\n{backup}')
        except (sqlite3.Error, OSError, ValueError) as exc:
            messagebox.showerror('削除失敗', str(exc))

    def has_unsaved_changes(self):
        return self.original is not None and (self.official.get(), self.ocr.get()) != self.original[1:]

    def export_dialog(self):
        if self.has_unsaved_changes():
            messagebox.showwarning('未保存の変更', '曲名を保存または再読込してからエクスポートしてください。')
            return
        dialog = tk.Toplevel(self.root)
        dialog.title('エクスポート範囲')
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        area = ttk.Frame(dialog, padding=18)
        area.pack(fill='both', expand=True)
        ttk.Label(area, text='ZIPに songs.csv と normal_charts.csv を格納します。').pack(anchor='w', pady=(0, 10))
        choice = tk.StringVar(value='all')
        ttk.Radiobutton(area, text='全件（すべての曲・譜面）', variable=choice, value='all').pack(anchor='w', pady=4)
        selected_name = ''
        if self.selected_id is not None:
            selected_name = next((r[1] for r in self.rows if r[0] == self.selected_id), '')
        selected = ttk.Radiobutton(area, text=f'選択曲のみ：{selected_name or "未選択"}', variable=choice, value='selected')
        selected.pack(anchor='w', pady=4)
        if not selected_name:
            selected.configure(state='disabled')
        def proceed():
            ids = None if choice.get() == 'all' else [self.selected_id]
            dialog.destroy()
            self.export_zip(ids)
        ttk.Button(area, text='保存先を指定', command=proceed).pack(anchor='e', pady=(12, 0))
        dialog.wait_window()

    def export_zip(self, selected_ids=None):
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        name = f'iidx_database_{stamp}.zip'
        dest = filedialog.asksaveasfilename(parent=self.root, title='エクスポートZIPの保存先',
            defaultextension='.zip', initialfile=name, filetypes=[('ZIPファイル', '*.zip')])
        if not dest:
            return
        try:
            if selected_ids is None:
                songs = self.conn.execute('SELECT song_id, official_title, ocr_title FROM songs ORDER BY song_id').fetchall()
                charts = self.conn.execute('SELECT song_id, difficulty, level, note_events FROM normal_charts ORDER BY song_id, difficulty').fetchall()
            else:
                songs = self.conn.execute('SELECT song_id, official_title, ocr_title FROM songs WHERE song_id=?', (selected_ids[0],)).fetchall()
                charts = self.conn.execute('SELECT song_id, difficulty, level, note_events FROM normal_charts WHERE song_id=? ORDER BY difficulty', (selected_ids[0],)).fetchall()
                if not songs:
                    raise ValueError('選択曲が見つかりません。')
            def make_csv(headers, records):
                output = io.StringIO(newline='')
                writer = csv.writer(output, lineterminator='\r\n')
                writer.writerow(headers)
                writer.writerows(records)
                return output.getvalue().encode('utf-8-sig')
            with zipfile.ZipFile(dest, 'w', compression=zipfile.ZIP_DEFLATED) as z:
                z.writestr('songs.csv', make_csv(['song_id', 'official_title', 'ocr_title'], songs))
                z.writestr('normal_charts.csv', make_csv(['song_id', 'difficulty', 'level', 'note_events'], charts))
            messagebox.showinfo('エクスポート完了', f'曲: {len(songs)}件 / 譜面: {len(charts)}件\n\n{dest}')
        except (OSError, sqlite3.Error, ValueError, zipfile.BadZipFile) as exc:
            messagebox.showerror('エクスポート失敗', str(exc))

    def import_dialog(self):
        if self.has_unsaved_changes():
            messagebox.showwarning('未保存の変更', '曲名を保存または再読込してからインポートしてください。')
            return
        notice = (
            'インポート対象は ZIP ファイル1つです。\n'
            'ZIP内の必須ファイル名：\n'
            '  ・songs.csv（曲名一覧）\n'
            '  ・normal_charts.csv（譜面イベント）\n\n'
            'エクスポートZIPのファイル名例：\n'
            '  iidx_database_20261011_213500.zip\n\n'
            '【取込ルール】\n'
            '・正式曲名が完全一致した曲は、既存song_idを維持してOCR曲名を更新します。\n'
            '・一致しない曲は新規登録し、song_idを自動採番します。\n'
            '・譜面のsong_idは取込先のsong_idへ変換します。\n'
            '・同一曲・同一難易度の譜面はレベル・イベントを上書きします。\n'
            '・ZIPにない曲や譜面は削除しません。\n'
            '・既存DBがある場合は実行前にdb_backups/へバックアップを作成します。\n'
            '・DBがない場合は検証後の初回インポート時に新規作成します。\n\n'
            'ファイル選択後、変更件数を表示し、再度確認します。\n'
            'インポートを続けますか？'
        )
        if not messagebox.askokcancel('インポートの注意事項', notice, parent=self.root):
            return
        source = filedialog.askopenfilename(parent=self.root, title='インポートするZIPファイルを選択',
            filetypes=[('IIDXデータZIP', '*.zip')])
        if not source:
            return
        try:
            songs, charts = self.parse_import_zip(source)
            summary = self.plan_import(songs, charts)
        except (ValueError, OSError, UnicodeError, csv.Error, json.JSONDecodeError, zipfile.BadZipFile, sqlite3.Error) as exc:
            messagebox.showerror('インポート検証エラー', f'データベースは変更していません。\n\n{exc}')
            return
        confirm = (f'ファイル：{Path(source).name}\n\n'
                   f'新規曲：{summary["new_songs"]}件\n'
                   f'既存曲の照合：{summary["matched_songs"]}件（OCR曲名を更新）\n'
                   f'新規譜面：{summary["new_charts"]}件\n'
                   f'既存譜面上書き：{summary["updated_charts"]}件\n\n'
                   '上書き対象の既存データが変更されます。\n'
                   + ('既存DBをバックアップして実行しますか？' if not self.db_missing
                    else '新しいSQLiteデータベースを作成して実行しますか？'))
        if not messagebox.askyesno('インポート最終確認', confirm, parent=self.root):
            return
        try:
            backup = self.apply_import(songs, charts)
            self.selected_id = None
            self.original = None
            self.official.set('')
            self.ocr.set('')
            self.load_rows()
            detail = f'バックアップ：\n{backup}' if backup is not None else f'新規作成したDB：\n{DB}（既存DBなし・バックアップ不要）'
            messagebox.showinfo('インポート完了', f'曲: {len(songs)}件 / 譜面: {len(charts)}件\n\n{detail}')
        except (ValueError, OSError, sqlite3.Error) as exc:
            messagebox.showerror('インポート失敗', f'インポートを完了できませんでした。\n{exc}')

    @staticmethod
    def parse_import_zip(source):
        required = {'songs.csv', 'normal_charts.csv'}
        with zipfile.ZipFile(source) as z:
            names = z.namelist()
            if len(names) != len(set(names)) or set(names) != required:
                raise ValueError('ZIPには直下の songs.csv と normal_charts.csv の2ファイルのみを入れてください。')
            if any(z.getinfo(name).file_size > 100 * 1024 * 1024 for name in required):
                raise ValueError('CSVファイルが大きすぎます（上限100MB/ファイル）。')
            def read_csv(name, headers):
                with z.open(name) as file:
                    text = io.TextIOWrapper(file, encoding='utf-8-sig', newline='')
                    reader = csv.DictReader(text, strict=True)
                    if reader.fieldnames != headers:
                        raise ValueError(f'{name} の列名が不正です。必要: {headers}')
                    rows = list(reader)
                if any(None in row or any(v is None for v in row.values()) for row in rows):
                    raise ValueError(f'{name} に列数の不一致があります。')
                return rows
            song_rows = read_csv('songs.csv', ['song_id', 'official_title', 'ocr_title'])
            chart_rows = read_csv('normal_charts.csv', ['song_id', 'difficulty', 'level', 'note_events'])
        ids, titles = set(), set()
        songs = []
        for row in song_rows:
            sid = int(row['song_id'])
            title, ocr = row['official_title'], row['ocr_title']
            if sid <= 0 or sid in ids or not title.strip() or not ocr.strip() or title in titles:
                raise ValueError('songs.csv に不正なID・空欄・重複曲名があります。')
            ids.add(sid)
            titles.add(title)
            songs.append((sid, title, ocr))
        chart_keys = set()
        charts = []
        for row in chart_rows:
            sid = int(row['song_id'])
            difficulty = row['difficulty']
            level = int(row['level'])
            if sid not in ids:
                raise ValueError(f'譜面のsong_id={sid} がsongs.csvにありません。')
            if difficulty not in ('BEGINNER', 'NORMAL', 'HYPER', 'ANOTHER', 'LEGGENDARIA') or not 1 <= level <= 12:
                raise ValueError(f'譜面の難易度またはレベルが不正です: song_id={sid}')
            key = (sid, difficulty)
            if key in chart_keys:
                raise ValueError(f'重複譜面: {key}')
            chart_keys.add(key)
            events = json.loads(row['note_events'])
            if not isinstance(events, list) or not events:
                raise ValueError(f'譜面イベントが空または配列ではありません: {key}')
            for n, event in enumerate(events, 1):
                if not isinstance(event, dict) or event.get('event_no') != n:
                    raise ValueError(f'イベント番号が不正です: {key}, #{n}')
                lanes = event.get('event_lanes')
                if not isinstance(lanes, list) or not lanes or any(type(v) is not int or v < 1 or v > 7 for v in lanes) or len(set(lanes)) != len(lanes):
                    raise ValueError(f'イベントレーンが不正です: {key}, #{n}')
            charts.append((sid, difficulty, level, row['note_events']))
        return songs, charts

    def plan_import(self, songs, charts):
        db_songs = {title: sid for sid, title in self.conn.execute('SELECT song_id, official_title FROM songs')}
        db_charts = set(self.conn.execute('SELECT song_id, difficulty FROM normal_charts'))
        source_titles = {sid: title for sid, title, _ in songs}
        new_charts = updated_charts = 0
        for sid, diff, _, _ in charts:
            title = source_titles[sid]
            if title in db_songs and (db_songs[title], diff) in db_charts:
                updated_charts += 1
            else:
                new_charts += 1
        matched = sum(title in db_songs for _, title, _ in songs)
        return dict(new_songs=len(songs)-matched, matched_songs=matched,
                    new_charts=new_charts, updated_charts=updated_charts)

    def apply_import(self, songs, charts):
        backup = None
        if not self.db_missing:
            BACKUP.mkdir(exist_ok=True)
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            backup = BACKUP / f'iidx_songs_before_import_{stamp}.sqlite3'
            dest = sqlite3.connect(str(backup))
            try:
                self.conn.backup(dest)
            finally:
                dest.close()
        mapping = {}
        try:
            self.conn.execute('BEGIN IMMEDIATE')
            for source_id, title, ocr in songs:
                row = self.conn.execute('SELECT song_id FROM songs WHERE official_title=?', (title,)).fetchone()
                if row:
                    dest_id = row[0]
                    self.conn.execute('UPDATE songs SET ocr_title=? WHERE song_id=?', (ocr, dest_id))
                else:
                    cur = self.conn.execute('INSERT INTO songs(official_title,ocr_title) VALUES (?,?)', (title, ocr))
                    dest_id = cur.lastrowid
                mapping[source_id] = dest_id
            for source_id, difficulty, level, events in charts:
                self.conn.execute("""INSERT INTO normal_charts(song_id,difficulty,level,note_events)
                    VALUES (?,?,?,?) ON CONFLICT(song_id,difficulty)
                    DO UPDATE SET level=excluded.level, note_events=excluded.note_events""",
                    (mapping[source_id], difficulty, level, events))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        if self.db_missing:
            # Publish a fully populated SQLite file atomically. If the write
            # fails, the target path remains absent and the in-memory DB stays
            # available for a retry.
            if DB.exists():
                raise FileExistsError(f'インポート中に別の処理がDBを作成しました: {DB}')
            fd, tmp_name = tempfile.mkstemp(prefix='.iidx_songs_', suffix='.sqlite3', dir=str(BASE))
            os.close(fd)
            temp_path = Path(tmp_name)
            try:
                dest = sqlite3.connect(str(temp_path))
                try:
                    self.conn.backup(dest)
                finally:
                    # Context managers commit/rollback but do not close SQLite connections.
                    # Windows requires this handle to be closed before os.replace().
                    dest.close()
                if DB.exists():
                    raise FileExistsError(f'インポート中に別の処理がDBを作成しました: {DB}')
                os.replace(temp_path, DB)
                new_conn = sqlite3.connect(f'file:{DB.as_posix()}?mode=rw', uri=True, timeout=3)
                new_conn.execute('PRAGMA foreign_keys=ON')
                self.conn.close()
                self.conn = new_conn
                self.db_missing = False
            finally:
                temp_path.unlink(missing_ok=True)
        return backup

    def close(self):
        if self.original is not None and (self.official.get(), self.ocr.get()) != self.original[1:]:
            if not messagebox.askyesno('未保存の変更', '未保存の変更を破棄して終了しますか？'):
                return
        if self.conn is not None:
            self.conn.close()
        self.root.destroy()

if __name__ == '__main__':
    root = tk.Tk()
    Editor(root)
    root.mainloop()
