"""Offline IIDX history overlay position editor (1920x1080 screenshot coordinates)."""
import json
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from PIL import Image, ImageTk
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from iidx_monitor import options

from iidx_paths import app_root
BASE = app_root()
CONFIG = BASE / 'config' / 'iidx_history_overlay_config.json'
DEFAULT = {'x': 30, 'y': 150, 'width': 510, 'height': 360}
PHASES = {'プレー中': 'playing', 'プレー終了後': 'ended', '選曲中': 'selecting'}
PREVIEW = [
    {'title': 'London Affairs Beckoned With Money Loved By Yellow Papers.', 'difficulty': 'ANOTHER', 'level': 8, 'layout': '1674523'},
    {'title': 'Second Heaven', 'difficulty': 'HYPER', 'level': 9, 'layout': '3275614'},
    {'title': 'Donkey Donk', 'difficulty': 'NORMAL', 'level': 5, 'layout': '7341625'},
]


def load_config():
    profiles = {side: {'positions': {key: DEFAULT.copy() for key in PHASES.values()},
                       'visibility': {key: True for key in PHASES.values()}}
                for side in ('1P', '2P')}
    raw = {}
    if CONFIG.is_file():
        try:
            raw = json.loads(CONFIG.read_text(encoding='utf-8-sig'))
            legacy = raw.get('positions', {})
            if not legacy and 'x' in raw:
                legacy = {'playing': raw}
            for side in profiles:
                for phase in PHASES.values():
                    vals = raw.get('side_profiles', {}).get(side, {}).get('positions', {}).get(phase,
                           legacy.get(phase, {}))
                    for key in DEFAULT:
                        if key in vals:
                            profiles[side]['positions'][phase][key] = int(vals[key])
                    enabled = raw.get('side_profiles', {}).get(side, {}).get('visibility', {}).get(phase, True)
                    profiles[side]['visibility'][phase] = bool(enabled)
        except (ValueError, OSError, TypeError, KeyError) as exc:
            raise ValueError(f'設定を読み込めません: {exc}') from exc
    return profiles, raw


class Editor:
    def __init__(self, root):
        self.root = root
        root.title('IIDX 履歴オーバーレイ 設定')
        self.profiles, self.raw = load_config()
        self.monitor_options = options()
        for side in ('1P', '2P'):
            self.profiles[side]['monitor'] = str(self.raw.get('side_profiles', {}).get(side, {}).get('monitor', ''))
        self.side = '1P'
        self.positions = self.profiles[self.side]['positions']
        self.phase = 'playing'
        self.cfg = self.positions[self.phase]
        self.vars = {k: tk.StringVar(value=str(v)) for k, v in self.cfg.items()}
        self.image = None
        self.tkimage = None
        self.scale = 1.0
        self.drag = None
        self.handle_radius = 7
        self.image_path = None
        self.screen_w = 1920
        self.screen_h = 1080
        self.status = tk.StringVar(value=f'保存先: {CONFIG}')
        self.canvas = tk.Canvas(root, bg='#202328', highlightthickness=0)
        bar = ttk.Frame(root, padding=8)
        bar.pack(side='top', fill='x')
        ttk.Label(bar, text='調整対象:').pack(side='left', padx=3)
        self.side_choice = tk.StringVar(value='1P')
        for side in ('1P', '2P'):
            ttk.Radiobutton(bar, text=side, variable=self.side_choice, value=side, command=self.switch_side).pack(side='left', padx=4)
        ttk.Label(bar, text='フェーズ:').pack(side='left', padx=(14, 3))
        self.phase_choice = ttk.Combobox(bar, values=list(PHASES), state='readonly', width=12)
        self.phase_choice.set('プレー中')
        self.phase_choice.pack(side='left', padx=3)
        self.phase_choice.bind('<<ComboboxSelected>>', self.switch_phase)
        ttk.Button(bar, text='背景画像を開く', command=self.open_image).pack(side='left', padx=(16, 3))
        ttk.Button(bar, text='背景画像を解除', command=self.clear_image).pack(side='left', padx=3)
        ttk.Button(bar, text='設定を保存', command=self.save).pack(side='right', padx=4)
        ttk.Button(bar, text='初期値に戻す', command=self.reset).pack(side='right', padx=4)

        monbar = ttk.Frame(root, padding=(10, 2, 10, 4))
        monbar.pack(fill='x')
        ttk.Label(monbar, text='表示先モニター:').pack(side='left', padx=5)
        self.monitor_choice = tk.StringVar()
        self.monitor_box = ttk.Combobox(monbar, state='disabled', width=68,
                                        values=[label for label, _ in self.monitor_options],
                                        textvariable=self.monitor_choice)
        self.monitor_box.pack(side='left', padx=5)
        ttk.Button(monbar, text='再検出', command=self.refresh_monitors, state='disabled').pack(side='left', padx=5)
        ttk.Label(monbar, text='（履歴はメインモニター固定・選択不可）').pack(side='left', padx=5)

        opts = ttk.Frame(root, padding=(10, 2, 10, 5))
        opts.pack(fill='x')
        self.visible_var = tk.BooleanVar(value=self.profiles[self.side]['visibility'][self.phase])
        ttk.Checkbutton(opts, text='このサイド・フェーズを表示', variable=self.visible_var,
                        command=self.change_visible).pack(side='left', padx=5)
        for key, label in [('x', 'X'), ('y', 'Y'), ('width', '幅'), ('height', '高さ')]:
            ttk.Label(opts, text=label).pack(side='left', padx=(10, 2))
            ent = ttk.Entry(opts, textvariable=self.vars[key], width=7)
            ent.pack(side='left')
            ent.bind('<Return>', self.on_edit)
        ttk.Button(opts, text='入力値を反映', command=self.on_edit).pack(side='left', padx=10)
        for label, dx, dy in [('← 1px', -1, 0), ('→ 1px', 1, 0), ('↑ 1px', 0, -1), ('↓ 1px', 0, 1)]:
            ttk.Button(opts, text=label, command=lambda a=dx, b=dy: self.nudge(a, b)).pack(side='left', padx=3)

        ttk.Label(root, text='枠内ドラッグで移動、四隅・各辺の白いハンドルでサイズ変更。座標は1920×1080基準です。', padding=(10, 2)).pack(side='top', anchor='w')
        self.canvas.pack(fill='both', expand=True, padx=8, pady=8)
        self.canvas.bind('<ButtonPress-1>', self.mouse_down)
        self.canvas.bind('<B1-Motion>', self.mouse_move)
        self.canvas.bind('<ButtonRelease-1>', lambda e: setattr(self, 'drag', None))
        self.canvas.bind('<Configure>', lambda e: self.redraw())
        ttk.Label(root, textvariable=self.status, padding=(12, 2, 12, 8), wraplength=1250).pack(fill='x')
        root.geometry('1320x860')
        candidates = [BASE / '3(4).png', BASE / 'play_screenshot.png']
        for p in candidates:
            if p.is_file():
                self.set_image(p)
                break
        self.load_monitor()
        self.redraw()

    def refresh_monitors(self):
        self.monitor_options = options()
        self.monitor_box.configure(values=[label for label, _ in self.monitor_options])
        self.load_monitor()

    def load_monitor(self):
        device = self.profiles[self.side].get('monitor', '')
        labels = [label for label, value in self.monitor_options if value == device]
        self.monitor_choice.set(labels[0] if labels else '未接続: ' + device)

    def change_monitor(self, event=None):
        for label, device in self.monitor_options:
            if label == self.monitor_choice.get():
                self.profiles[self.side]['monitor'] = device
                break

    def switch_side(self, event=None):
        self.on_edit()
        self.side = self.side_choice.get()
        self.positions = self.profiles[self.side]['positions']
        self.cfg = self.positions[self.phase]
        self.visible_var.set(self.profiles[self.side]['visibility'][self.phase])
        self.sync_vars()
        self.load_monitor()
        self.redraw()

    def change_visible(self):
        self.profiles[self.side]['visibility'][self.phase] = self.visible_var.get()
        self.redraw()

    def switch_phase(self, event=None):
        self.on_edit()
        self.phase = PHASES[self.phase_choice.get()]
        self.cfg = self.positions[self.phase]
        self.visible_var.set(self.profiles[self.side]['visibility'][self.phase])
        self.sync_vars()
        self.redraw()

    def set_image(self, path):
        try:
            with Image.open(path) as im:
                image = im.convert('RGB')
            if image.size != (1920, 1080):
                messagebox.showwarning('画像サイズ', f'画像は{image.width}×{image.height}です。1920×1080の画像を推奨します。')
            self.image = image
            self.screen_w, self.screen_h = image.size
            self.image_path = path
            self.redraw()
        except (OSError, ValueError) as exc:
            messagebox.showerror('画像を開けません', str(exc))

    def open_image(self):
        path = filedialog.askopenfilename(title='背景画像', filetypes=[('画像', '*.png *.jpg *.jpeg *.bmp')])
        if path:
            self.set_image(Path(path))

    def clear_image(self):
        self.image = None
        self.tkimage = None
        self.image_path = None
        self.screen_w, self.screen_h = 1920, 1080
        self.redraw()

    def on_edit(self, event=None):
        try:
            vals = {k: int(v.get()) for k, v in self.vars.items()}
            if vals['width'] < 150 or vals['height'] < 80:
                return
            self.cfg.update(vals)
            self.redraw()
        except ValueError:
            pass

    def nudge(self, dx, dy):
        self.on_edit()
        self.cfg['x'] += dx
        self.cfg['y'] += dy
        self.sync_vars()
        self.redraw()

    def reset(self):
        self.cfg.update(DEFAULT)
        self.sync_vars()
        self.redraw()

    def sync_vars(self):
        for k, v in self.cfg.items():
            self.vars[k].set(str(v))

    def transform(self):
        w = max(self.canvas.winfo_width() - 8, 1)
        h = max(self.canvas.winfo_height() - 8, 1)
        s = min(w / self.screen_w, h / self.screen_h)
        ox = (self.canvas.winfo_width() - self.screen_w * s) / 2
        oy = (self.canvas.winfo_height() - self.screen_h * s) / 2
        return s, ox, oy

    def redraw(self):
        c = self.canvas
        c.delete('all')
        s, ox, oy = self.transform()
        self.scale = s
        if self.image is not None:
            size = (max(1, round(self.screen_w*s)), max(1, round(self.screen_h*s)))
            self.tkimage = ImageTk.PhotoImage(self.image.resize(size, Image.Resampling.LANCZOS))
            c.create_image(ox, oy, anchor='nw', image=self.tkimage)
        else:
            # Other settings editors use a dark blue 1920x1080 preview area.
            c.create_rectangle(ox, oy, ox + self.screen_w*s, oy + self.screen_h*s,
                               fill='#101820', outline='#8a97a8')
            c.create_text(ox + self.screen_w*s/2, oy + 30,
                          text='「背景画像を開く」から1920×1080の画像を選択', fill='white')
        x = ox + self.cfg['x'] * s
        y = oy + self.cfg['y'] * s
        w = self.cfg['width'] * s
        h = self.cfg['height'] * s
        self.box = (x, y, x+w, y+h)
        c.create_rectangle(x, y, x+w, y+h, fill='#181818', outline='#38d6ff', width=2, tags='overlay')
        font_size = max(7, round(12*s))
        lines = ['RANDOM HISTORY']
        for i, entry in enumerate(PREVIEW, 1):
            lines.extend([f'{i}. {entry["title"]}', f'    {entry["difficulty"]} LV.{entry["level"]}  |  {entry["layout"]}'])
        c.create_text(x+12*s, y+8*s, text='\n'.join(lines), anchor='nw', fill='white',
                      font=('Yu Gothic UI', font_size), width=max(40, w-24*s), tags='overlay')
        c.create_text(x+w-4, y+h-4, anchor='se', text=f'({self.cfg["x"]}, {self.cfg["y"]})',
                      fill='#38d6ff', font=('Arial', max(8, round(11*s)), 'bold'), tags='overlay')
        for name, (hx, hy) in self.handle_points().items():
            r = 4
            c.create_rectangle(hx-r, hy-r, hx+r, hy+r, fill='white', outline='#38d6ff', width=1, tags='handle')

    def handle_points(self):
        x1, y1, x2, y2 = self.box
        xm, ym = (x1+x2)/2, (y1+y2)/2
        return {'nw': (x1,y1), 'n': (xm,y1), 'ne': (x2,y1),
                'w': (x1,ym), 'e': (x2,ym),
                'sw': (x1,y2), 's': (xm,y2), 'se': (x2,y2)}

    def mouse_down(self, event):
        for name, (hx, hy) in self.handle_points().items():
            if abs(event.x-hx) <= self.handle_radius and abs(event.y-hy) <= self.handle_radius:
                self.drag = ('resize', name, event.x, event.y, self.cfg.copy())
                return
        x1, y1, x2, y2 = self.box
        if x1 <= event.x <= x2 and y1 <= event.y <= y2:
            self.drag = ('move', None, event.x, event.y, self.cfg.copy())

    def mouse_move(self, event):
        if self.drag is None:
            return
        mode, handle, mx, my, original = self.drag
        dx = round((event.x-mx)/self.scale)
        dy = round((event.y-my)/self.scale)
        if mode == 'move':
            self.cfg['x'] = original['x'] + dx
            self.cfg['y'] = original['y'] + dy
        else:
            left, top = original['x'], original['y']
            right = left + original['width']
            bottom = top + original['height']
            if 'w' in handle:
                left = min(left + dx, right - 150)
            if 'e' in handle:
                right = max(right + dx, left + 150)
            if 'n' in handle:
                top = min(top + dy, bottom - 80)
            if 's' in handle:
                bottom = max(bottom + dy, top + 80)
            self.cfg.update(x=left, y=top, width=right-left, height=bottom-top)
        self.sync_vars()
        self.redraw()

    def save(self):
        self.on_edit()
        self.change_visible()
        try:
            if not (0 <= self.cfg['x'] <= self.screen_w-self.cfg['width'] and
                    0 <= self.cfg['y'] <= self.screen_h-self.cfg['height']):
                if not messagebox.askyesno('画面外', '履歴表示の一部が画面外になります。そのまま保存しますか？'):
                    return
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            data = dict(self.raw)
            data['positions'] = self.profiles['1P']['positions']
            data['side_profiles'] = self.profiles
            tmp = CONFIG.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
            tmp.replace(CONFIG)
            self.status.set(f'保存しました: {CONFIG}（メイン再起動後に反映）')
            messagebox.showinfo('保存完了', f'保存しました:\n{CONFIG}\n\n分析ツールを再起動すると反映されます。')
        except OSError as exc:
            messagebox.showerror('保存失敗', str(exc))


def main():
    root = tk.Tk()
    try:
        Editor(root)
    except ValueError as exc:
        messagebox.showerror('設定エラー', str(exc))
        root.destroy()
        return
    root.mainloop()


if __name__ == '__main__':
    main()
