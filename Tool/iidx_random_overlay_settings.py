"""Standalone RANDOM result overlay editor; screenshot preview and 1P/2P profiles.
Python 3.10+, Pillow. Current runtime consumes top-level x/y/scale/enabled (1P).
2P editor values are saved in editor_2p for the current runtime.
"""
import ctypes
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from iidx_monitor import options

from iidx_paths import app_root
ROOT = app_root()
CONFIG = ROOT / 'config' / 'iidx_overlay_config.json'
LEGACY = ROOT / 'iidx_overlay_config.json'
W, H = 1920, 1080
KEY_W, KEY_H, GAP, PAD_X, PAD_Y = 58, 72, 6, 14, 12
SAMPLE = '1735462'
DEFAULT = {'x': 160, 'y': 532, 'scale': 0.74, 'enabled': True}


def dpi_setup():
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass


def parse_profile(d, fallback):
    p = {}
    for key in ('x', 'y'):
        p[key] = int(d.get(key, fallback[key]))
    p['scale'] = float(d.get('scale', fallback['scale']))
    p['enabled'] = bool(d.get('enabled', fallback['enabled']))
    p['monitor'] = str(d.get('monitor', fallback.get('monitor', '')))
    return p


class Editor:
    def __init__(self, root):
        self.root = root
        root.title('IIDX RANDOM分析結果 表示設定')
        source = CONFIG if CONFIG.exists() else LEGACY
        try:
            data = json.loads(source.read_text(encoding='utf-8-sig')) if source.exists() else {}
            if not isinstance(data, dict):
                raise ValueError('設定JSONはオブジェクトである必要があります')
            self.data = data
            first = parse_profile(data, DEFAULT)
            second = parse_profile(data.get('editor_2p', {}), first)
        except (ValueError, TypeError, OSError) as exc:
            messagebox.showwarning('設定読み込み', f'設定の読み込みに失敗しました。初期値を使用します。\n{exc}')
            self.data = {}
            first, second = DEFAULT.copy(), DEFAULT.copy()
        self.profiles = {'1P': first, '2P': second}
        self.side = tk.StringVar(value='1P')
        self.xvar = tk.StringVar()
        self.yvar = tk.StringVar()
        self.svar = tk.StringVar()
        self.enabled = tk.BooleanVar()
        self.monitor_options = options()
        self.monitor_choice = tk.StringVar()
        self.image = None
        self.tkimage = None
        self.image_path = None
        self.drag = None
        self.handle_radius = 9
        self.scale = 1.0
        self.offset = (0, 0)
        self.rect = (0, 0, 0, 0)
        self.status = tk.StringVar(value=f'保存先: {CONFIG}')

        bar = ttk.Frame(root, padding=9)
        bar.pack(fill='x')
        ttk.Label(bar, text='調整対象:').pack(side='left', padx=3)
        for side in ('1P', '2P'):
            ttk.Radiobutton(bar, text=side, value=side, variable=self.side, command=self.switch_side).pack(side='left', padx=4)
        ttk.Button(bar, text='背景画像を開く', command=self.open_image).pack(side='left', padx=12)
        ttk.Button(bar, text='背景画像を解除', command=self.clear_image).pack(side='left', padx=3)
        ttk.Button(bar, text='設定を保存', command=self.save).pack(side='right', padx=4)
        ttk.Button(bar, text='初期値に戻す', command=self.reset).pack(side='right', padx=4)

        monbar = ttk.Frame(root, padding=(9, 2, 9, 4))
        monbar.pack(fill='x')
        ttk.Label(monbar, text='表示先モニター:').pack(side='left', padx=5)
        self.monitor_box = ttk.Combobox(monbar, state='readonly', width=68,
                                        values=[label for label, _ in self.monitor_options],
                                        textvariable=self.monitor_choice)
        self.monitor_box.pack(side='left', padx=5)
        self.monitor_box.bind('<<ComboboxSelected>>', self.change_monitor)
        ttk.Button(monbar, text='再検出', command=self.refresh_monitors).pack(side='left', padx=5)

        opts = ttk.Frame(root, padding=(9, 2, 9, 7))
        opts.pack(fill='x')
        ttk.Checkbutton(opts, text='直接オーバーレイを表示', variable=self.enabled, command=self.toggle_enabled).pack(side='left', padx=5)
        ttk.Label(opts, text='X').pack(side='left', padx=(15, 2))
        xentry = ttk.Entry(opts, textvariable=self.xvar, width=8)
        xentry.pack(side='left')
        xentry.bind('<Return>', lambda _e: self.apply_entries())
        ttk.Label(opts, text='Y').pack(side='left', padx=(12, 2))
        yentry = ttk.Entry(opts, textvariable=self.yvar, width=8)
        yentry.pack(side='left')
        yentry.bind('<Return>', lambda _e: self.apply_entries())
        ttk.Label(opts, text='倍率').pack(side='left', padx=(12, 2))
        sentry = ttk.Entry(opts, textvariable=self.svar, width=8)
        sentry.pack(side='left')
        sentry.bind('<Return>', lambda _e: self.apply_entries())
        ttk.Button(opts, text='入力値を反映', command=self.apply_entries).pack(side='left', padx=10)
        for label, dx, dy in [('← 1px', -1, 0), ('→ 1px', 1, 0), ('↑ 1px', 0, -1), ('↓ 1px', 0, 1)]:
            ttk.Button(opts, text=label, command=lambda a=dx, b=dy: self.nudge(a, b)).pack(side='left', padx=3)

        self.notice = ttk.Label(root, text='', padding=(12, 3), wraplength=1200, justify='left')
        self.notice.pack(fill='x')
        self.canvas = tk.Canvas(root, bg='#202328', highlightthickness=0)
        self.canvas.pack(fill='both', expand=True, padx=9, pady=5)
        self.canvas.bind('<Configure>', lambda _e: self.draw())
        self.canvas.bind('<Button-1>', self.mouse_down)
        self.canvas.bind('<B1-Motion>', self.mouse_drag)
        self.canvas.bind('<ButtonRelease-1>', lambda _e: setattr(self, 'drag', None))

        ttk.Label(root, textvariable=self.status, padding=(12, 2, 12, 8), wraplength=1250).pack(anchor='w')
        root.geometry('1320x850')
        self.load_fields()

    def refresh_monitors(self):
        self.monitor_options = options()
        self.monitor_box.configure(values=[label for label, _ in self.monitor_options])
        self.load_monitor()

    def load_monitor(self):
        device = self.current().get('monitor', '')
        labels = [label for label, value in self.monitor_options if value == device]
        if labels:
            self.monitor_choice.set(labels[0])
        else:
            self.monitor_choice.set('未接続: ' + device)

    def change_monitor(self, event=None):
        label = self.monitor_choice.get()
        for name, device in self.monitor_options:
            if name == label:
                self.current()['monitor'] = device
                break

    def current(self):
        return self.profiles[self.side.get()]

    def load_fields(self):
        self.load_monitor()
        p = self.current()
        self.xvar.set(str(p['x']))
        self.yvar.set(str(p['y']))
        self.svar.set(f"{p['scale']:.2f}")
        self.enabled.set(p['enabled'])
        self.notice.config(text=(
            '1P: 現行メインプログラムに反映（再起動後）。2P: 現行メインプログラムに反映（再起動後）。'
            ' 画像はプレビュー専用。表示枠内をドラッグして移動、右下の白いハンドルで倍率を変更できます。'
        ))
        self.draw()

    def apply_entries(self, silent=False):
        try:
            x, y, scale = int(self.xvar.get()), int(self.yvar.get()), float(self.svar.get())
            if not 0.5 <= scale <= 2.0:
                raise ValueError('倍率は0.50～2.00です')
            if not -10000 <= x <= 10000 or not -10000 <= y <= 10000:
                raise ValueError('X・Yは -10000～10000 です')
            self.current().update(x=x, y=y, scale=scale, enabled=self.enabled.get())
            self.draw()
            return True
        except (ValueError, OverflowError) as exc:
            if not silent:
                messagebox.showerror('入力エラー', f'X・Yは整数、倍率は0.50～2.00で入力してください。\n{exc}')
            return False

    def switch_side(self):
        # Values from previous side are already committed on button / nudge / drag.
        # Preserve manual edits in fields before changing side via tracked last side.
        old = getattr(self, '_last_side', '1P')
        if old != self.side.get():
            try:
                x, y, s = int(self.xvar.get()), int(self.yvar.get()), float(self.svar.get())
                if .5 <= s <= 2:
                    self.profiles[old].update(x=x, y=y, scale=s, enabled=self.enabled.get())
            except ValueError:
                pass
        self._last_side = self.side.get()
        self.load_fields()

    def reset(self):
        self.profiles[self.side.get()].update(DEFAULT)
        self.load_fields()

    def toggle_enabled(self):
        self.current()['enabled'] = self.enabled.get()
        self.draw()

    def open_image(self):
        filename = filedialog.askopenfilename(title='1920×1080のゲーム画面画像を選択', filetypes=[('画像ファイル', '*.png *.jpg *.jpeg *.bmp *.webp')])
        if not filename:
            return
        try:
            with Image.open(filename) as src:
                img = src.convert('RGB')
            if img.size != (W, H):
                messagebox.showerror('画像サイズ', f'1920×1080の画像を選択してください。選択画像: {img.width}×{img.height}')
                return
            self.image = img
            self.image_path = filename
            self.draw()
        except (OSError, ValueError) as exc:
            messagebox.showerror('画像読み込みエラー', str(exc))

    def clear_image(self):
        self.image = None
        self.image_path = None
        self.tkimage = None
        self.draw()

    def panel_dims(self, p):
        s = p['scale']
        kw, kh, gap = round(KEY_W*s), round(KEY_H*s), round(GAP*s)
        px, py = round(PAD_X*s), round(PAD_Y*s)
        return kw*7 + gap*6 + px*2, kh + py*2, kw, kh, gap, px, py

    def draw(self):
        c = self.canvas
        c.delete('all')
        cw, ch = c.winfo_width(), c.winfo_height()
        if cw < 20 or ch < 20:
            return
        scale = min(cw/W, ch/H)
        ox, oy = (cw-W*scale)/2, (ch-H*scale)/2
        self.scale, self.offset = scale, (ox, oy)
        if self.image is not None:
            dims = (max(1, round(W*scale)), max(1, round(H*scale)))
            self.tkimage = ImageTk.PhotoImage(self.image.resize(dims, Image.Resampling.LANCZOS))
            c.create_image(ox, oy, image=self.tkimage, anchor='nw')
        else:
            c.create_rectangle(ox, oy, ox+W*scale, oy+H*scale, fill='#101820', outline='#8a97a8')
            c.create_text(ox+W*scale/2, oy+30, text='「背景画像を開く」から1920×1080の画像を選択', fill='white')
        p = self.current()
        w, h, kw, kh, gap, px, py = self.panel_dims(p)
        x, y = ox+p['x']*scale, oy+p['y']*scale
        self.rect = (x, y, x+w*scale, y+h*scale)
        c.create_rectangle(*self.rect, fill='', outline='#ffcc00', width=3, tags='panel')
        for i, num in enumerate(SAMPLE):
            xx, yy = x+(px+i*(kw+gap))*scale, y+py*scale
            light = int(num)%2 == 1
            c.create_rectangle(xx, yy, xx+kw*scale, yy+kh*scale,
                               fill='#f5f5f5' if light else '#111116', outline='#dedede', tags='panel')
            c.create_text(xx+kw*scale/2, yy+kh*scale/2, text=num,
                          fill='#111111' if light else '#ffffff',
                          font=('Arial', max(8, round(25*p['scale']*scale*2)), 'bold'), tags='panel')
        hx, hy = self.rect[2], self.rect[3]
        c.create_rectangle(hx-5, hy-5, hx+5, hy+5, fill='white', outline='#ffcc00', width=2, tags='handle')
        if not p['enabled']:
            c.create_text(ox+W*scale/2, oy+H*scale/2, text='表示OFF（編集・保存は可能）',
                          fill='#ffda61', font=('Arial', 18, 'bold'))
        c.create_text(ox+5, oy+H*scale-5, anchor='sw', fill='#ffdc62',
                      text=f"{self.side.get()}  X={p['x']}  Y={p['y']}  倍率={p['scale']:.2f}  表示={'ON' if p['enabled'] else 'OFF'}")

    def mouse_down(self, event):
        x1, y1, x2, y2 = self.rect
        p = self.current()
        if abs(event.x-x2) <= self.handle_radius and abs(event.y-y2) <= self.handle_radius:
            self.drag = ('resize', event.x, event.y, p['x'], p['y'], p['scale'])
        elif x1 <= event.x <= x2 and y1 <= event.y <= y2:
            self.drag = ('move', event.x, event.y, p['x'], p['y'], p['scale'])

    def mouse_drag(self, event):
        if self.drag is None:
            return
        mode, sx, sy, x, y, original_scale = self.drag
        p = self.current()
        if mode == 'move':
            p['x'] = x + round((event.x-sx)/self.scale)
            p['y'] = y + round((event.y-sy)/self.scale)
            self.xvar.set(str(p['x']))
            self.yvar.set(str(p['y']))
        else:
            # Compare cursor displacement with original panel size, not a
            # repeatedly rounded preview rectangle, to prevent scale drift.
            original_w = (KEY_W*7 + GAP*6 + PAD_X*2) * original_scale
            original_h = (KEY_H + PAD_Y*2) * original_scale
            delta_x = (event.x-sx)/self.scale
            delta_y = (event.y-sy)/self.scale
            # Dominant drag direction controls uniform scale.
            if abs(delta_x / max(original_w, 1)) >= abs(delta_y / max(original_h, 1)):
                factor = (original_w + delta_x) / max(original_w, 1)
            else:
                factor = (original_h + delta_y) / max(original_h, 1)
            p['scale'] = round(max(0.5, min(2.0, original_scale * factor)), 3)
            self.svar.set(f"{p['scale']:.3f}".rstrip('0').rstrip('.'))
        self.draw()

    def nudge(self, dx, dy):
        if not self.apply_entries():
            return
        p = self.current()
        p['x'] += dx
        p['y'] += dy
        self.xvar.set(str(p['x']))
        self.yvar.set(str(p['y']))
        self.draw()

    def save(self):
        if not self.apply_entries():
            return
        try:
            # Preserve unknown settings, including future configuration keys.
            existing = json.loads(CONFIG.read_text(encoding='utf-8-sig')) if CONFIG.exists() else self.data.copy()
            if not isinstance(existing, dict):
                raise ValueError('設定JSONがオブジェクトではありません')
            existing.update(self.profiles['1P'])
            second = existing.get('editor_2p', {})
            if not isinstance(second, dict):
                second = {}
            second.update(self.profiles['2P'])
            existing['editor_2p'] = second
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
            tmp.replace(CONFIG)
            self.data = existing
            self.status.set(f'保存しました: {CONFIG}（1P・2Pともメイン再起動後に反映）')
            messagebox.showinfo('保存完了', '1P・2P両方の設定を保存しました。\n1P・2Pともメインプログラム再起動後に反映します。')
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror('保存エラー', str(exc))


if __name__ == '__main__':
    dpi_setup()
    root = tk.Tk()
    Editor(root)
    root.mainloop()
