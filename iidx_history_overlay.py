"""Optional history overlay. Uses the existing analyzer Tk event pump."""
from iidx_paths import app_root
import ctypes
import json
from pathlib import Path
import tkinter as tk
from iidx_monitor import offset, place_window

def load_history_geometry(config_path=None, phase="PLAYING", side="1P"):
    """Load optional screen-pixel geometry; fall back to the existing defaults."""
    defaults = {'x': 30, 'y': 150, 'width': 510, 'height': 360}
    path = Path(config_path) if config_path is not None else app_root() / 'config' / 'iidx_history_overlay_config.json'
    if not path.is_file():
        return defaults
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict):
            raise ValueError('config must be an object')
        monitor_device = data.get('side_profiles', {}).get(side, {}).get('monitor', '')
        if 'positions' in data:
            state = phase_to_key(phase)
            data = (data.get('side_profiles', {}).get(side, {}).get('positions', {}).get(state)
                    or data['positions'].get(state, data['positions'].get('playing', defaults)))
        values = {k: int(data.get(k, v)) for k, v in defaults.items()}
        if values['width'] < 100 or values['height'] < 50:
            raise ValueError('invalid width or height')
        dx, dy = offset(monitor_device, 'HISTORY')
        values['x'] += dx
        values['y'] += dy
        return values
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        print(f'[HISTORY OVERLAY CONFIG WARNING] {path}: {exc}; using defaults', flush=True)
        return defaults


def phase_to_key(phase):
    if phase in ('PRE_PLAY', 'PLAYING', 'CONFIRMED'):
        return 'playing'
    if phase == 'ENDED':
        return 'ended'
    return 'selecting'


class DirectHistoryOverlay:
    def __init__(self, parent, x=30, y=150, width=510):
        geometry = load_history_geometry()
        x, y, width, height = (geometry[k] for k in ('x', 'y', 'width', 'height'))
        print(f'[HISTORY OVERLAY POSITION] x={x} y={y} width={width} height={height}', flush=True)
        self.current_geometry = geometry
        self.current_phase_key = None
        self.side = '1P'
        self.win = tk.Toplevel(parent)
        self.win.title('IIDX RANDOM History')
        self.win.overrideredirect(True)
        self.win.attributes('-topmost', True)
        self.transparent = '#010203'
        self.win.configure(bg=self.transparent)
        self.win.wm_attributes('-transparentcolor', self.transparent)
        place_window(self.win, x, y, width, height)
        self.frame = tk.Frame(self.win, bg='#181818', padx=12, pady=8)
        self.frame.pack(fill='both', expand=True)
        self.label = tk.Label(self.frame, text='', justify='left', anchor='nw',
                              bg='#181818', fg='white', font=('Yu Gothic UI', 12),
                              wraplength=width-28)
        self.label.pack(fill='both', expand=True)
        self.win.update_idletasks()
        hwnd = self.win.winfo_id()
        try:
            u = ctypes.windll.user32
            ex = u.GetWindowLongW(hwnd, -20)
            u.SetWindowLongW(hwnd, -20, ex | 0x20 | 0x80 | 0x08000000)
            u.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x1 | 0x2 | 0x10 | 0x40)
        except (AttributeError, OSError):
            pass
        self.last_key = None
        self.win.withdraw()

    def set_side(self, side):
        side = side if side in ('1P', '2P') else '1P'
        if side != self.side:
            self.side = side
            self.current_phase_key = None
            self.last_key = None
            print(f'[HISTORY SIDE APPLIED] side={side}', flush=True)

    def side_visible(self, phase):
        path = app_root() / 'config' / 'iidx_history_overlay_config.json'
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            return data.get('side_profiles', {}).get(self.side, {}).get('visibility', {}).get(phase_to_key(phase), True) is not False
        except (OSError, ValueError, TypeError, AttributeError):
            return True

    def update_history(self, history, visible, phase='PLAYING'):
        phase_key = phase_to_key(phase)
        if phase_key != self.current_phase_key:
            self.current_phase_key = phase_key
            geometry = load_history_geometry(phase=phase, side=self.side)
            if geometry != self.current_geometry:
                self.current_geometry = geometry
                x, y, width, height = (geometry[k] for k in ('x', 'y', 'width', 'height'))
                place_window(self.win, x, y, width, height)
                self.win.update_idletasks()
                self.label.configure(wraplength=width-28)
                print(f'[HISTORY OVERLAY POSITION] phase={phase_key} x={x} y={y} width={width} height={height}', flush=True)
        visible = bool(visible) and self.side_visible(phase)
        key = (self.side, phase_key, bool(visible), tuple((e.get('title'), e.get('difficulty'), e.get('level'),
                                     e.get('layout'), e.get('status')) for e in history))
        if key == self.last_key:
            return
        self.last_key = key
        if not visible or not history:
            self.win.withdraw()
            print(f'[HISTORY WINDOW] phase={phase_key} action=HIDE visible_setting={bool(visible)} entries={len(history)}', flush=True)
            return
        lines = ['RANDOM HISTORY']
        for i, entry in enumerate(history):
            title = str(entry.get('title') or 'UNKNOWN')
            diff = str(entry.get('difficulty') or '?')
            lv = entry.get('level')
            layout = entry.get('layout') or '未確定'
            lines.append(f'{len(history) - i}. {title}\n    {diff} LV.{lv if lv is not None else "?"}  |  {layout}')
        self.label.config(text='\n'.join(lines))
        self.win.deiconify()
        # Tk can restore its own cached geometry when a withdrawn Toplevel is
        # shown again. Apply the selected monitor's virtual-desktop coordinates
        # *after* deiconify, on the real wrapper HWND (iidx_monitor v2).
        geometry = load_history_geometry(phase=phase, side=self.side)
        self.current_geometry = geometry
        x, y, width, height = (geometry[k] for k in ('x', 'y', 'width', 'height'))
        place_window(self.win, x, y, width, height)
        self.label.configure(wraplength=width-28)
        self.win.lift()
        self.win.attributes('-topmost', True)
        self.win.update_idletasks()
        print(f'[HISTORY MONITOR POSITION] side={self.side} phase={phase_key} '
              f'x={x} y={y} width={width} height={height}', flush=True)
        print(f'[HISTORY WINDOW] phase={phase_key} action=SHOW '
              f'geometry={self.win.winfo_geometry()} '
              f'state={self.win.state()} '
              f'viewable={self.win.winfo_viewable()} '
              f'visible_setting={bool(visible)} entries={len(history)}', flush=True)

    def close(self):
        self.win.destroy()
