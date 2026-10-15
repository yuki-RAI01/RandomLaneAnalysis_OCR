# Direct overlay reused from the previously working integrated overlay version.
import ctypes
import json
import tkinter as tk
from pathlib import Path
from iidx_monitor import place_window

class DirectRandomOverlay:
    """
    Lightweight Win32/Tk overlay output.
    Does not touch Spout, GET READY, v5 detection, or RANDOM analysis logic.
    """

    GWL_EXSTYLE = -20
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000
    HWND_TOPMOST = -1
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOACTIVATE = 0x0010
    SWP_SHOWWINDOW = 0x0040

    def __init__(self, x=650, y=820, scale=1.0):
        self.user32 = ctypes.windll.user32
        self.root = tk.Tk()
        self.root.title("IIDX RANDOM Direct Overlay")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)

        self.scale = max(0.50, min(2.00, float(scale)))
        self.key_w = max(1, round(58 * self.scale))
        self.key_h = max(1, round(72 * self.scale))
        self.key_gap = max(0, round(6 * self.scale))
        self.pad_x = max(0, round(14 * self.scale))
        self.pad_y = max(0, round(12 * self.scale))
        self.font_size = max(8, round(30 * self.scale))
        self.border_width = max(1, round(2 * self.scale))

        self.transparent_key = "#010203"
        self.panel_bg = "#171717"
        self.white_key = "#f5f5f5"
        self.black_key = "#0f0f12"
        self.white_text = "#111111"
        self.black_text = "#ffffff"
        self.border = "#eeeeee"

        self.keys_w = self.key_w * 7 + self.key_gap * 6
        self.panel_w = self.keys_w + self.pad_x * 2
        self.panel_h = self.key_h + self.pad_y * 2

        self.root.configure(bg=self.transparent_key)
        self.root.wm_attributes("-transparentcolor", self.transparent_key)
        place_window(self.root, x, y, self.panel_w, self.panel_h)

        self.canvas = tk.Canvas(
            self.root,
            width=self.panel_w,
            height=self.panel_h,
            bg=self.panel_bg,
            bd=0,
            highlightthickness=0,
        )
        self.canvas.place(x=0, y=0, width=self.panel_w, height=self.panel_h)

        self.root.update_idletasks()
        self.hwnd = self.root.winfo_id()
        self._apply_window_style()

        # Start hidden. RANDOM confirmation makes it visible.
        self.visible = True
        self.clear()
        self.pump()

    def apply_profile(self, x, y, scale, enabled=True):
        """Apply a side-specific saved profile without recreating the Tk root.

        The history overlay is a child of this root, so it must remain alive.
        """
        self.clear()
        self.enabled = bool(enabled)
        self.scale = max(0.50, min(2.00, float(scale)))
        self.key_w = max(1, round(58 * self.scale))
        self.key_h = max(1, round(72 * self.scale))
        self.key_gap = max(0, round(6 * self.scale))
        self.pad_x = max(0, round(14 * self.scale))
        self.pad_y = max(0, round(12 * self.scale))
        self.font_size = max(8, round(30 * self.scale))
        self.border_width = max(1, round(2 * self.scale))
        self.keys_w = self.key_w * 7 + self.key_gap * 6
        self.panel_w = self.keys_w + self.pad_x * 2
        self.panel_h = self.key_h + self.pad_y * 2
        place_window(self.root, x, y, self.panel_w, self.panel_h)
        self.canvas.configure(width=self.panel_w, height=self.panel_h)
        self.canvas.place_configure(width=self.panel_w, height=self.panel_h)
        self.root.update_idletasks()

    def _apply_window_style(self):
        exstyle = self.user32.GetWindowLongW(self.hwnd, self.GWL_EXSTYLE)
        exstyle |= (
            self.WS_EX_TRANSPARENT
            | self.WS_EX_TOOLWINDOW
            | self.WS_EX_NOACTIVATE
        )
        self.user32.SetWindowLongW(self.hwnd, self.GWL_EXSTYLE, exstyle)
        self._force_topmost()

    def _force_topmost(self):
        self.user32.SetWindowPos(
            self.hwnd,
            self.HWND_TOPMOST,
            0, 0, 0, 0,
            self.SWP_NOMOVE
            | self.SWP_NOSIZE
            | self.SWP_NOACTIVATE
            | self.SWP_SHOWWINDOW,
        )

    def pump(self):
        # Tk is deliberately serviced from the analyzer's existing main loop,
        # so no extra worker thread touches the proven detector/capture path.
        self.root.update_idletasks()
        self.root.update()

    def clear(self):
        self.canvas.delete("all")
        self.root.withdraw()
        self.visible = False

    def show_layout(self, layout):
        if not getattr(self, "enabled", True):
            self.clear()
            return
        safe_layout = "".join(ch for ch in str(layout) if ch in "1234567")[:7]
        if len(safe_layout) != 7:
            return

        self.canvas.delete("all")
        self.canvas.configure(bg=self.panel_bg)

        for i, number in enumerate(safe_layout):
            x1 = self.pad_x + i * (self.key_w + self.key_gap)
            y1 = self.pad_y
            x2 = x1 + self.key_w
            y2 = y1 + self.key_h

            odd = int(number) % 2 == 1
            fill = self.white_key if odd else self.black_key
            text_color = self.white_text if odd else self.black_text

            self.canvas.create_rectangle(
                x1, y1, x2, y2,
                fill=fill,
                outline=self.border,
                width=self.border_width,
            )
            self.canvas.create_text(
                (x1 + x2) / 2,
                (y1 + y2) / 2,
                text=number,
                fill=text_color,
                font=("Arial", self.font_size, "bold"),
            )

        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self._force_topmost()
        self.visible = True
        self.pump()

    def close(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

def load_direct_overlay_config(path, fallback_x=650, fallback_y=820, fallback_scale=1.0):
    """
    Load position/scale saved by iidx_direct_overlay_position_adjuster.
    Missing or invalid config safely falls back to command-line/default values.
    """
    path = Path(path)
    x = int(fallback_x)
    y = int(fallback_y)
    scale = float(fallback_scale)

    if not path.is_file():
        print(f"[DIRECT OVERLAY CONFIG] not found: {path}")
        print(
            "[DIRECT OVERLAY CONFIG] using fallback: "
            f"x={x} y={y} scale={scale:.2f}"
        )
        return x, y, scale

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        x = int(data.get("x", x))
        y = int(data.get("y", y))
        scale = float(data.get("scale", scale))
        scale = max(0.50, min(2.00, scale))
    except Exception as exc:
        print(f"[DIRECT OVERLAY CONFIG] load failed: {exc}")
        print(
            "[DIRECT OVERLAY CONFIG] using fallback: "
            f"x={x} y={y} scale={scale:.2f}"
        )
        return x, y, scale

    print(
        f"[DIRECT OVERLAY CONFIG] loaded: {path.resolve()} "
        f"x={x} y={y} scale={scale:.2f}"
    )
    return x, y, scale


def write_overlay_state(path, confirmed=False, layout=""):
    """Atomically publish tiny JS state for the OBS browser overlay."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_layout = "".join(ch for ch in str(layout) if ch in "1234567")[:7]
    text = (
        "window.IIDX_RANDOM_STATE = "
        + json.dumps(
            {"confirmed": bool(confirmed), "layout": safe_layout},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + ";\n"
    )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
