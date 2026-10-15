"""Windows monitor selection shared by overlay editors and runtime.
Saved monitor device names are stable across most display-order changes.
Overlay x/y remain local to the selected monitor; legacy configs stay absolute.
"""
import ctypes
import sys
from ctypes import wintypes

class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT),
                ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

def monitors():
    if sys.platform != "win32":
        return []
    user32 = ctypes.windll.user32
    result = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                      ctypes.POINTER(RECT), wintypes.LPARAM)
    def callback(handle, hdc, rect, data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            r = info.rcMonitor
            result.append(dict(device=info.szDevice, x=r.left, y=r.top,
                               width=r.right-r.left, height=r.bottom-r.top,
                               primary=bool(info.dwFlags & 1)))
        return True
    fn = callback_type(callback)
    user32.EnumDisplayMonitors(None, None, fn, 0)
    result.sort(key=lambda m: (not m['primary'], m['x'], m['y']))
    return result

def options():
    return [("従来どおり（モニター指定なし）", "")] + [
        (f"モニター{i}: {m['width']}×{m['height']}" + ("（メイン）" if m['primary'] else "")
         + f" [{m['device']}]", m['device'])
        for i, m in enumerate(monitors(), 1)]

def _display_number(device):
    """Extract DISPLAYn even if a saved path contains extra slash escaping."""
    import re
    match = re.search(r"DISPLAY\s*(\d+)\s*$", str(device).upper())
    return int(match.group(1)) if match else None


def offset(device, label="OVERLAY"):
    """Return selected monitor origin, with conservative DISPLAYn recovery."""
    if not device:
        return 0, 0
    items = monitors()
    # Exact match first, then case/whitespace normalization.
    for m in items:
        if m['device'] == device or m['device'].strip().casefold() == str(device).strip().casefold():
            return m['x'], m['y']
    # Some stored device names have differently escaped Windows path prefixes.
    # DISPLAYn is the Windows display device ID, not the sorted GUI index.
    wanted = _display_number(device)
    matches = [m for m in items if wanted is not None and _display_number(m['device']) == wanted]
    if len(matches) == 1:
        m = matches[0]
        print(f"[{label} MONITOR RECOVERED] saved={device!r} actual={m['device']!r} "
              f"origin=({m['x']},{m['y']})", flush=True)
        return m['x'], m['y']
    primary = next((m for m in items if m['primary']), None)
    print(f"[{label} MONITOR WARNING] saved={device!r} not found; "
          f"connected={[(m['device'], m['x'], m['y'], m['primary']) for m in items]!r}; "
          f"using primary monitor", flush=True)
    return (primary['x'], primary['y']) if primary else (0, 0)

def place_window(window, x, y, width, height):
    """Position a Tk top-level in virtual-screen pixels, including negative x/y.

    Tk's geometry manager owns the outer HWND. winfo_id() can refer to the
    inner/client HWND, so SetWindowPos(winfo_id()) can silently move the wrong
    window while geometry('+0+0') pins the actual top-level to the origin.
    """
    x, y, width, height = map(int, (x, y, width, height))
    # Tk geometry supports signed positions, but a negative position in the
    # normal X geometry syntax is measured from the RIGHT/BOTTOM edge.
    # Let Win32 set exact virtual-desktop coordinates on the wrapper HWND.
    if sys.platform == "win32":
        window.geometry(f"{width}x{height}+0+0")
        window.update_idletasks()
        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, wintypes.UINT]
        user32.SetWindowPos.restype = wintypes.BOOL
        inner = wintypes.HWND(window.winfo_id())
        wrapper = user32.GetParent(inner)
        hwnd = wrapper if wrapper else inner
        # SWP_NOZORDER | SWP_NOACTIVATE: preserve topmost and focus state.
        ok = user32.SetWindowPos(hwnd, None, x, y, width, height, 0x0004 | 0x0010)
        if not ok:
            print(f"[OVERLAY POSITION WARNING] SetWindowPos failed: "
                  f"winerror={ctypes.get_last_error()} x={x} y={y}", flush=True)
    else:
        window.geometry(f"{width}x{height}{x:+d}{y:+d}")
