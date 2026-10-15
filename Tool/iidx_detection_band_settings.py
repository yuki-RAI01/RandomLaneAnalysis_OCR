"""Standalone note detection band Y editor. Python 3.10+, Pillow required.

Existing runtime reads config/roi_default.json -> roi and band_y_in_roi.
This editor preserves unrelated JSON keys. 2P settings are saved under editor_2p.
"""
import json
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk

import sys
APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from iidx_paths import app_root
BASE = app_root()
CONFIG = BASE / 'config' / 'roi_default.json'
DEFAULT_1P = [165, 84, 343, 81]
FRAME_W, FRAME_H = 1920, 1080
BAND_HEIGHT = 20  # Matches current main program (band_top + 20).


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def load_data():
    if CONFIG.is_file():
        data = json.loads(CONFIG.read_text(encoding='utf-8-sig'))
    else:
        data = {'roi': DEFAULT_1P[:], 'band_y_in_roi': [55, 75],
                'lane_numbering': 'left_to_right_1_to_7', 'scratch_detection': False}
    roi = data.get('roi', DEFAULT_1P)
    if not isinstance(roi, list) or len(roi) != 4:
        raise ValueError('roi は4要素の配列である必要があります')
    return data


class Editor:
    def __init__(self, root):
        self.root = root
        self.root.title('IIDX 検出帯 設定')
        self.data = load_data()
        self.roi1 = list(map(int, self.data['roi']))
        # 2P horizontal preset is a geometric mirror, pending runtime verification.
        self.roi2 = list(map(int, self.data.get('editor_2p', {}).get('roi', [FRAME_W - self.roi1[0] - self.roi1[2], self.roi1[1], self.roi1[2], self.roi1[3]])))
        self.tops = {'1P': int(self.data.get('band_y_in_roi', [55])[0]),
                     '2P': int(self.data.get('editor_2p', {}).get('band_y_in_roi', [55])[0])}
        self.side = tk.StringVar(value='1P')
        self.y_var = tk.IntVar(value=self.tops['1P'])
        self.image = None
        self.image_path = None
        self.tkimage = None
        self.scale = 1
        self.offset = (0, 0)
        self.dragging = False
        self.changed = False
        self.status = tk.StringVar(value=f'保存先: {CONFIG}')

        bar = ttk.Frame(root, padding=8)
        bar.pack(fill='x')
        ttk.Label(bar, text='調整対象:').pack(side='left', padx=4)
        for side in ('1P', '2P'):
            ttk.Radiobutton(bar, text=side, variable=self.side, value=side, command=self.switch_side).pack(side='left', padx=4)
        ttk.Button(bar, text='背景画像を開く', command=self.open_image).pack(side='left', padx=(16, 3))
        ttk.Button(bar, text='背景画像を解除', command=self.clear_image).pack(side='left', padx=3)
        ttk.Button(bar, text='設定を保存', command=self.save).pack(side='right', padx=4)
        ttk.Button(bar, text='初期値に戻す', command=self.reset).pack(side='right', padx=4)

        settings = ttk.Frame(root, padding=(10, 2))
        settings.pack(fill='x')
        ttk.Label(settings, text='検出帯 上端（ROI内Y）').pack(side='left')
        self.spin = ttk.Spinbox(settings, from_=0, to=1000, width=6, textvariable=self.y_var)
        self.spin.pack(side='left', padx=6)
        self.spin.bind('<Return>', self.from_input)
        ttk.Button(settings, text='入力値を反映', command=self.from_input).pack(side='left', padx=6)
        ttk.Button(settings, text='↑ 1px', command=lambda: self.shift(-1)).pack(side='left', padx=3)
        ttk.Button(settings, text='↓ 1px', command=lambda: self.shift(1)).pack(side='left', padx=3)
        self.info = ttk.Label(settings, text='')
        self.info.pack(side='left', padx=16)

        self.notice = ttk.Label(root, padding=(10, 5), wraplength=1250, justify='left',
            text='画像上の色付き帯を上下ドラッグできます（横移動・帯の高さ変更は不可）。背景画像はプレビュー専用です。')
        self.notice.pack(fill='x')
        self.canvas = tk.Canvas(root, bg='#202328', highlightthickness=0)
        self.canvas.pack(fill='both', expand=True, padx=8, pady=8)
        self.canvas.bind('<Configure>', lambda _e: self.redraw())
        self.canvas.bind('<Button-1>', self.mouse_down)
        self.canvas.bind('<B1-Motion>', self.mouse_drag)
        self.canvas.bind('<ButtonRelease-1>', lambda _e: setattr(self, 'dragging', False))
        ttk.Label(root, textvariable=self.status, padding=(12, 2, 12, 8), wraplength=1250).pack(fill='x')
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.geometry('1320x850')
        self.refresh()

    def roi(self):
        return self.roi1 if self.side.get() == '1P' else self.roi2

    def limit(self, value):
        roi = self.roi()
        # Keep the entire detection band inside image and ensure ROI covers it.
        return clamp(int(value), 0, FRAME_H - roi[1] - BAND_HEIGHT)

    def refresh(self):
        side = self.side.get()
        self.tops[side] = self.limit(self.tops[side])
        self.y_var.set(self.tops[side])
        x, y, w, h = self.roi()
        top = self.tops[side]
        self.info.config(text=f'{side}  X={x}（固定）  絶対Y={y + top}～{y + top + BAND_HEIGHT}  高さ={BAND_HEIGHT}px')
        if side == '2P':
            self.notice.config(text='2Pの横位置は1P領域を左右反転した仮の固定値です。2P設定はiidx_main.pyで読み込まれます。横位置は保存済みのROI値です。')
        else:
            self.notice.config(text='画像上の色付き帯を上下ドラッグできます（横移動・帯の高さ変更は不可）。1P設定は現行メインプログラムが次回起動時に読み込みます。')
        self.redraw()

    def switch_side(self):
        self.refresh()

    def from_input(self, _event=None):
        try:
            value = self.limit(self.y_var.get())
        except (ValueError, tk.TclError):
            self.refresh()
            return
        self.tops[self.side.get()] = value
        self.changed = True
        self.refresh()

    def shift(self, delta):
        side = self.side.get()
        self.tops[side] = self.limit(self.tops[side] + delta)
        self.changed = True
        self.refresh()

    def reset(self):
        side = self.side.get()
        original = self.data.get('band_y_in_roi', [55]) if side == '1P' else self.data.get('editor_2p', {}).get('band_y_in_roi', [55])
        self.tops[side] = self.limit(int(original[0]))
        self.refresh()

    def open_image(self):
        name = filedialog.askopenfilename(title='IIDXプレー画面（1920×1080）', filetypes=[('画像ファイル', '*.png *.jpg *.jpeg *.bmp *.webp')])
        if not name:
            return
        try:
            with Image.open(name) as img:
                loaded = img.convert('RGB')
            if loaded.size != (FRAME_W, FRAME_H):
                messagebox.showerror('画像サイズ', f'1920×1080の画像を指定してください。選択画像は {loaded.width}×{loaded.height} です。')
                return
            self.image = loaded
            self.image_path = name
            self.redraw()
        except OSError as exc:
            messagebox.showerror('読み込み失敗', str(exc))

    def clear_image(self):
        self.image = None
        self.image_path = None
        self.tkimage = None
        self.redraw()

    def redraw(self):
        canvas = self.canvas
        canvas.delete('all')
        cw, ch = canvas.winfo_width(), canvas.winfo_height()
        if cw < 10 or ch < 10:
            return
        scale = min(cw / FRAME_W, ch / FRAME_H)
        ox, oy = (cw - FRAME_W * scale) / 2, (ch - FRAME_H * scale) / 2
        self.scale, self.offset = scale, (ox, oy)
        if self.image is not None:
            size = (max(1, round(FRAME_W * scale)), max(1, round(FRAME_H * scale)))
            self.tkimage = ImageTk.PhotoImage(self.image.resize(size, Image.Resampling.LANCZOS))
            canvas.create_image(ox, oy, image=self.tkimage, anchor='nw')
        else:
            canvas.create_rectangle(ox, oy, ox + FRAME_W * scale, oy + FRAME_H * scale, fill='#101820', outline='#687582')
            canvas.create_text(ox + FRAME_W * scale / 2, oy + 30, text='背景画像を開くとプレー画面上で調整できます', fill='white')
        x, y, w, _h = self.roi()
        top = self.tops[self.side.get()]
        # Canvas stipple is used so the underlying screenshot remains visible.
        canvas.create_rectangle(ox + x*scale, oy + y*scale, ox + (x+w)*scale, oy + (y + max(self.roi()[3], top+BAND_HEIGHT))*scale,
                                outline='#4cb9ff', width=2)
        canvas.create_rectangle(ox + x*scale, oy + (y+top)*scale, ox + (x+w)*scale, oy + (y+top+BAND_HEIGHT)*scale,
                                fill='#f8dc45', stipple='gray25', outline='#ffb300', width=2, tags='band')
        canvas.create_text(ox + x*scale, oy + (y+top)*scale - 9, anchor='sw', text=f'{self.side.get()} BAND Y={y+top}', fill='#ffdc43', font=('Arial', 11, 'bold'))

    def mouse_down(self, event):
        x, y, w, _ = self.roi()
        ox, oy = self.offset
        px = (event.x - ox) / self.scale
        py = (event.y - oy) / self.scale
        top = self.tops[self.side.get()]
        self.dragging = x <= px <= x+w and y+top-12 <= py <= y+top+BAND_HEIGHT+12
        if self.dragging:
            self.drag_offset = py - (y+top)

    def mouse_drag(self, event):
        if not self.dragging:
            return
        _x, y, _w, _h = self.roi()
        py = (event.y - self.offset[1]) / self.scale
        new_top = self.limit(round(py - self.drag_offset - y))
        if new_top != self.tops[self.side.get()]:
            self.tops[self.side.get()] = new_top
            self.changed = True
            self.refresh()

    def save(self):
        # Reload immediately before writing, preserving unrelated settings.
        try:
            current = load_data()
            if list(map(int, current['roi'])) != self.roi1:
                if not messagebox.askyesno('ROIの変更', '外部で1PのROIが変更されています。現在のROIを維持し、Y設定のみ保存しますか？'):
                    return
            top = int(self.tops['1P'])
            current['band_y_in_roi'] = [top, top + BAND_HEIGHT]
            current['editor_2p'] = {**current.get('editor_2p', {}), 'roi': self.roi2, 'band_y_in_roi': [int(self.tops['2P']), int(self.tops['2P']) + BAND_HEIGHT]}
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            temp = CONFIG.with_suffix('.json.tmp')
            temp.write_text(json.dumps(current, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            temp.replace(CONFIG)
            self.data = current
            self.changed = False
            self.status.set(f'保存しました: {CONFIG}（1P・2Pともメイン再起動後に反映）')
            messagebox.showinfo('保存完了', f'保存先: {CONFIG}\n1Pは次回メインプログラム起動時に反映されます。\n2Pもiidx_main.pyの次回起動時に読み込まれます。')
        except (OSError, ValueError, TypeError, KeyError) as exc:
            messagebox.showerror('保存失敗', str(exc))

    def close(self):
        if self.changed and not messagebox.askyesno('未保存の変更', '保存していない変更があります。終了しますか？'):
            return
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        Editor(root)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        messagebox.showerror('設定読み込みエラー', str(exc))
        root.destroy()
        return
    root.mainloop()


if __name__ == '__main__':
    main()
