import sys

# DPI experiment v1_4_5_12: run BEFORE cv2/pygame/Tkinter/Spout imports.
# This is process-wide and intentionally isolated to this test entry point.
def _configure_early_windows_dpi():
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        setter = user32.SetProcessDpiAwarenessContext
        setter.argtypes = [wintypes.HANDLE]
        setter.restype = wintypes.BOOL
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        for name, value in (("PER_MONITOR_AWARE_V2", -4), ("PER_MONITOR_AWARE", -3)):
            ctypes.set_last_error(0)
            if setter(wintypes.HANDLE(value)):
                print(f"[DPI BOOTSTRAP] requested={name} result=OK", flush=True)
                return
            error = ctypes.get_last_error()
            print(f"[DPI BOOTSTRAP] requested={name} result=FAILED winerror={error}", flush=True)
            # ERROR_ACCESS_DENIED means process DPI awareness is already set.
            # Do not attempt to override it or silently report success.
            if error == 5:
                print("[DPI BOOTSTRAP] awareness already set; leaving unchanged", flush=True)
                return
    except (AttributeError, OSError, ValueError) as exc:
        print(f"[DPI BOOTSTRAP] API unavailable: {exc!r}", flush=True)

_configure_early_windows_dpi()

import argparse
import csv
from datetime import datetime
import json
import sqlite3
import threading
import queue
import subprocess
import shutil
import difflib
import unicodedata
import time
import ctypes
import base64
import zlib
from pathlib import Path

import cv2
import numpy as np
import pygame
import SpoutGL
from pygame.locals import DOUBLEBUF, OPENGL
from OpenGL.GL import (
    GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T,
    GL_CLAMP_TO_EDGE, GL_TEXTURE_MAG_FILTER, GL_TEXTURE_MIN_FILTER,
    GL_LINEAR, GL_RGBA, GL_UNSIGNED_BYTE,
    glGenTextures, glBindTexture, glTexParameteri, glTexImage2D,
    glGetTexImage,
)

# Proven OBS -> SpoutGL sender used by the current integrated analyzer.
SENDER_NAME = "Spout_OBS_Filter"
INITIAL_W = 640
INITIAL_H = 360

# Fixed bottom-corner ROIs for 1920x1080 input.
# These correspond to the persistent "PLAYER 01 SIDE" / "PLAYER 02 SIDE"
# indicators shown after player entry on MODE SELECT / MUSIC SELECT, etc.
LEFT_SIDE_ROI_1920  = (0, 930, 280, 1015)       # x1, y1, x2, y2
RIGHT_SIDE_ROI_1920 = (1640, 930, 1920, 1015)  # x1, y1, x2, y2

TEMPLATE_01_SIDE = "PLAYER_01_SIDE.png"
TEMPLATE_02_SIDE = "PLAYER_02_SIDE.png"
TEMPLATE_NO_ENTRY = "NO_ENTRY.png"

# First test values. 3 consecutive frames are required before the side is locked.
SIDE_MIN_SCORE = 0.90
SIDE_CONFIRM_FRAMES = 3

def windows_quit_pressed():
    try:
        user32 = ctypes.windll.user32
        return bool(
            (user32.GetAsyncKeyState(0x1B) & 0x8000) or
            (user32.GetAsyncKeyState(ord("Q")) & 0x8000)
        )
    except Exception:
        return False


def allocate_texture(texture_id, width, height):
    glBindTexture(GL_TEXTURE_2D, texture_id)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
    glTexImage2D(
        GL_TEXTURE_2D, 0, GL_RGBA,
        width, height, 0,
        GL_RGBA, GL_UNSIGNED_BYTE, None,
    )


def cv_imread_unicode(path: Path, flags=cv2.IMREAD_UNCHANGED):
    """Read an image from a Windows path that may contain Japanese characters."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)

    # cv2.imread() can fail on some Windows/OpenCV combinations when the path
    # contains non-ASCII characters. Read the raw bytes with NumPy/Python, then
    # let OpenCV decode the image from memory instead.
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        raise OSError(f"Image file is empty or unreadable: {path}")

    img = cv2.imdecode(data, flags)
    if img is None:
        raise OSError(f"OpenCV could not decode image: {path}")
    return img


def load_template(path: Path):
    img = cv_imread_unicode(path, cv2.IMREAD_UNCHANGED)

    if img.ndim == 3 and img.shape[2] == 4:
        bgr = img[:, :, :3]
        alpha = img[:, :, 3]
        # Keep only genuinely visible pixels. TM_CCORR_NORMED supports masks.
        mask = np.where(alpha >= 16, 255, 0).astype(np.uint8)
    else:
        bgr = img[:, :, :3] if img.ndim == 3 else cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        mask = np.full(bgr.shape[:2], 255, dtype=np.uint8)

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return gray, mask


def scaled_roi(ref_roi, width, height):
    sx = width / 1920.0
    sy = height / 1080.0
    x1, y1, x2, y2 = ref_roi
    x1 = max(0, min(width - 1, int(round(x1 * sx))))
    y1 = max(0, min(height - 1, int(round(y1 * sy))))
    x2 = max(x1 + 1, min(width, int(round(x2 * sx))))
    y2 = max(y1 + 1, min(height, int(round(y2 * sy))))
    return x1, y1, x2, y2



def match_one(search_gray, templ_gray, templ_mask):
    if search_gray.shape[0] < templ_gray.shape[0] or search_gray.shape[1] < templ_gray.shape[1]:
        return -1.0, (0, 0)
    result = cv2.matchTemplate(
        search_gray, templ_gray, cv2.TM_CCORR_NORMED, mask=templ_mask
    )
    # Masked matching can theoretically create non-finite values at degenerate
    # locations. Make them harmless before minMaxLoc.
    result = np.nan_to_num(result, nan=-1.0, posinf=-1.0, neginf=-1.0)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    return float(max_val), max_loc



# ------------------------------------------------------------------
# PRE_PLAY scratch detector (from the proven small-ROI v2 test)
# ------------------------------------------------------------------
TEMPLATE_SCRATCH = "SCRATCH.png"

SCRATCH_EXPECTED_POS = {
    "1P": (21, 729),
    "2P": (1758, 729),
}
SCRATCH_MARGIN_X = 35
SCRATCH_MARGIN_Y = 30
SCRATCH_THRESHOLD = {
    "1P": 0.93,
    "2P": 0.83,
}
SCRATCH_CONFIRM_FRAMES = 3
SCRATCH_CENTER_MASK_RADIUS_RATIO = 0.29


def load_scratch_template(path: Path):
    img = cv_imread_unicode(path, cv2.IMREAD_UNCHANGED)

    if img.ndim == 2:
        bgr = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        alpha = np.full(img.shape, 255, dtype=np.uint8)
    elif img.shape[2] == 4:
        bgr = img[:, :, :3].copy()
        alpha = img[:, :, 3].copy()
    else:
        bgr = img[:, :, :3].copy()
        alpha = np.full(img.shape[:2], 255, dtype=np.uint8)

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    mask = np.where(alpha >= 32, 255, 0).astype(np.uint8)

    h, w = mask.shape
    cx, cy = w // 2, h // 2
    radius = max(1, int(round(min(w, h) * SCRATCH_CENTER_MASK_RADIUS_RATIO)))
    cv2.circle(mask, (cx, cy), radius, 0, -1)

    if cv2.countNonZero(mask) < 20:
        raise ValueError("SCRATCH template mask has too few visible pixels.")
    return gray, mask


def scratch_roi_1920(side, templ_w, templ_h):
    ex, ey = SCRATCH_EXPECTED_POS[side]
    return (
        max(0, ex - SCRATCH_MARGIN_X),
        max(0, ey - SCRATCH_MARGIN_Y),
        min(1920, ex + templ_w + SCRATCH_MARGIN_X),
        min(1080, ey + templ_h + SCRATCH_MARGIN_Y),
    )



# ------------------------------------------------------------------
# DIFFICULTY + LEVEL metadata detectors
#
# These coordinates were derived from the four supplied 1920x1080 PRE_PLAY
# layouts.  Graph position shifts the central play panel horizontally, so
# metadata ROIs are selected by the already-detected layout.
# ------------------------------------------------------------------

DIFFICULTY_ROI_BY_LAYOUT = {
    "1P_GRAPH_LEFT":  (970, 68, 1160, 100),
    "1P_GRAPH_RIGHT": (554, 68, 744, 100),
    "2P_GRAPH_LEFT":  (1188, 68, 1376, 100),
    "2P_GRAPH_RIGHT": (772, 68, 960, 100),
}

LEVEL_ROI_BY_LAYOUT = {
    "1P_GRAPH_LEFT":  (1066, 98, 1104, 119),
    "1P_GRAPH_RIGHT": (650, 98, 688, 119),
    "2P_GRAPH_LEFT":  (1280, 98, 1318, 119),
    "2P_GRAPH_RIGHT": (861, 98, 899, 119),
}

# ------------------------------------------------------------------
# SONG TITLE ROI test
# 1920x1080 coordinates measured from the four supplied London Affairs
# PRE_PLAY screenshots.  These target only the small, standardized title
# line at the top; the artist line and difficulty/level areas are excluded.
# ------------------------------------------------------------------
TITLE_ROI_BY_LAYOUT = {
    "1P_GRAPH_LEFT":  (1155, 55, 1725, 95),
    "1P_GRAPH_RIGHT": (738, 55, 1308, 95),
    "2P_GRAPH_LEFT":  (612, 55, 1182, 95),
    "2P_GRAPH_RIGHT": (196, 55, 766, 95),
}

TITLE_PREVIEW_SCALE = 2
SHOW_TITLE_PREVIEW = False
SAVE_TITLE_ROI = False
TITLE_WINDOW_NAME = "SONG TITLE ROI"

HUE_CENTERS = {
    "BEGINNER": 47.0,
    "NORMAL": 107.0,
    "HYPER": 19.0,
    "ANOTHER": 0.0,
    "LEGGENDARIA": 143.0,
}
DIFF_MIN_S = 100
DIFF_MIN_V = 70
DIFF_MAX_HUE_DISTANCE = 8.0
DIFF_MIN_COLOR_PIXELS = 150
METADATA_CONFIRM_FRAMES = 3

LEVEL_MIN_SCORE = 0.84
LEVEL_MIN_MARGIN = 0.035

TEMPLATE_H = 21
TEMPLATE_W = 38
_TEMPLATE_DATA = {
    1: 'eNql0jEOgCAMQFEWE8TBGMWBiQgicmYv5YlUBqCY0BB9Y/PbqYTUWDKC+hkdFowIu+9MQSM5rsdJWoUoVhpKlUYUqxWaQsVWxM/KQKkyiGK1QTxWG6JYWShWnc2ZqqrfgTlWe66pqrJbMvwXf1XWQZL5hnH3yQ3H1KkK',
    2: 'eNqV0tsNgjAUgOETiQZr1ESUGKjacBMYgyncgRk6ghswAROwRgdwAN99114gUC4Jfo/0z0lpCzCHJzjm4Lu5PWJP40xMQNem8DkLim8fq54RGBe/NVZxHwoGUUXAYchyHa1kR+EctNBwT0xUBVgqCCXi6lBTHUMNttZjs7QKy+TR7quU+8rBVeuRcAHICtb/xzeFlVqXwtXgKD6spADGrS7uHIZcrvBz7DJ2/r3jpEaxTrFABwdjrylibqSqbWIlaqtX3pfBTVX7JEns8XsU52Un0nJWJWaR6UdKVBWnHEETz5Ck//gBNObvmw==',
    3: 'eNqV0skNgkAUgOGXGHWAGIKBmMga9qEMqqAHaqAEO6ACK7ANCrAA7951lhd2Ev0SLryfyTAA8IuQM8j89lExLn44YawvsDv1XcQo0H1mnl3bsM6NBisV92qAYBEzZ6jqqZt8rAQ/HpDlpkRVw1UGieDZkon6yk4W1GGpUlQVeIsogLJFjzeP7kBwlA4cqCeHUQHY6YIHVYdkx9bCUTYyetUW9+WP574jmKqk9O/oyHnObZ+XK+Z5yi5z++wjWemUUm3jO9agUmm/XrF/gh3EIcSKr2Wt/197PcOI5gVjqfOCaFZQ/OsL2xDnIA==',
    4: 'eNqVkt8JwjAQh38gYkwVUVoRFPzba3UMp3AHZ3AEN+gETuACHSADOIDvvtfkbGrT5kG/hwTuvvw4jgC/sHGZCa4KvsR442XQyLDatsa8B7oVhhtfCv1tixFwepju84zMtXaWhR6Fe8WdUFk7l4kNel2AQdOKmaXUz1+mkxM6s9BaMq4RAUfFQVcgWMdeawhcOUgdgdDQtIhoDMo5KPsuylpUIcBjF3n2RblWkiQSXGujraQi1EvIcuXwdK1UE4rWJyjnCtIPxOcyGgZ1mtZo72FaWWWh+5PlzVrZ8aKykB58rKRxZHT4izcbn+1s',
    5: 'eNqV0tkNgjAYwPEvMSQcHkERIzHKWSljMAU7MAsbMAETuAADMIAD8M67tlAQSon6S3jqP6UXwC9cypLFg7LufljLk7DMIw6Qv4QqULzB98onWPVMeQns/QGrKsHyWREQhrCSttY1GGFVnY+kMQlVZ15x6iIGmRWIOEFWcdquScBEA3u+bFTSrIRNV9wpx1CnlH4/6n3EvUwduSqktPkvWaW14yEi3w1i/tSLhlY5GF21wxib4ntscgAbtyRx1VSPDA0RpnPZC49r3Uc4jAhb5YOVpp+96E9vRuL1LQ==',
    6: 'eNqVktFpwzAQho+CqeRCjAuhNXUKiW1ZykTZQTNkBG+gCTyB1vAA2cLviXQ6VNtRof1AepA+HXc/AvgLJ+RYlfw1dc3qU6TIfq3CUGgcroo20+2eAKAh3uGSNBZW27YMJn8wj/oZYC0BgM+uqbayb1S6riNLb42Xt88vfx8gy5oV+uJL1Vvridm68kETQkRrWhBODHBBkGXWXRlKApW+72P3PI9w0GT1RLQ+6h/WlpQyndeVLOkRUvKQ/d0Oi9DNjPMAR6tQag9iTGcxCtgrT+bWzk0y2Glt3Ozgpt+hpAq/HfL058oPQVLyjDRVuTFZWTXn//EA6TITgA==',
    7: 'eNqdzt0NgjAQwPFLiBHURvFJhWgoUFrGcIru4Azs5RoMwBa8Y/koraU1xN9Tc/3ncgBr4F7oW//88IGV0LnDi8ciFQKoO5u6gm06c1VdxyEeikw4A38ZqraPGgJJNlscT5opOoxBPrhHGqQilLvI6A3rotM8JAYtuhAHvLdHhS714Smja+EgIt5aI6pkekS/kDURPTIpUNGNGTbykfyImL5rsowYLSWCvL7ZReW/PnSsyHw=',
    8: 'eNqVksFtgzAUhn8pRXIggZaiqqE5RAGMnTEyRXfIDIzABkyQCbxABvAADMA9dwoP40CFovY7cHj+/Ou9h4G/cBzYR6/rl9kB88L4OCN2lyPYhxGSji+Al1fdtFP0rToDm2Skk6p7u4TiCElJ0zRERbVrcXkw3NNgqWENTdK8p4JuAqRkWeYOVgU/sgS4GCszBFB94TbPoi4aOKP1CU5hjaosqqbKN3xSeMcWKBdH7DZGQs+Y1dbaQlGURUqe56av+jxtiyvTV26wMzLXwuyMpAghvKf7Ej1ciMjsXpWP1ZeKdl+DkRVIeXj6H2WPI0lD8etN3LUqOeCTJIP+c9isFt+XGw+SFCeC7989ZyqsvLddcvofPwEaHYY=',
    9: 'eNqV0sFtgzAYhuFPldJAQlspUClS6KEB/thMlB08Q0bIBp7AE2SBDuABGCD33Cn8GBIrRmqfo3ltLNvAX+wHefa29D8s12m+92xX4SWirQuKTgLQ+dK0nubnTEBSjDYgc2tDDGHDSVmWEWw/dNPqTl94nkVUOgBPPPl7OvEgwElVVa5S2N1lUK6qnKlKs0k6V838kRMiimF491Y/sK2ryNkBOnwSU3XodBmUNtbTuOowojx5ebod9VgJIb5fQ3c4VqJHQsRQz/R1OPuYqw8p3UkEWMKn7C3mqqs1R+CdI9mttZp9ovGwkpSirotgtlinX/X//ALULRSa',
    10: 'eNqdktGNgjAYgP/EQ4uQM2rkcqUkHFZsHYMp3IEZHIENmIAJXKMDOIDvvHM/vVqb65l4fi9/+PhSmlKAZygYGQehBUKX4ez+ahYutUW+AuNokRB/EZLoikI9IC2sY4CqVZfBclFtBRBvEVutoDwPHucSVm5FQOHsu6Y2nLoehQLCObcVHMfZuJtqRnOEjPPUVnrWkKSGz5tJdzuvQvdD+u+K/a5YaWCOebXaG5hjvOqDGahTZbaqHp1XLoRdK4TuwdkLIaJblQC0/V//cSOlfLNVht84depqi6u+E+8YycXUbCKXeTzx7tecjpEU27l+yg9IydZRYIsg2mj7NN9xESVH',
    11: 'eNqt0j0OgzAMhuEsVIiFoS0DpRXk18nVeiXuSosEcWQj4YF3Sh592aKUJK31UK+HutdlFKdqe1DOCBrTq+/yb1Z3gzGUrKxFsBhDycq51w4PhzG8buX9kMFjDCWrEDI8A8ZQsgJ4ZwCMoBet2vjZoYsYwUq0aiPctq80FiuCkJJp1mszprJDPOkHh0DX/w==',
    12: 'eNqN0tsRgjAQBdD9QE3COAxKHOXhg4QIlGE71mIHVGAFNmABFGABVoEJiEBA5f7AZM9slgSAMQl9pB7IDUMbaTUD23JZ5TB5L7n2YBe0VsqFcyGTwRJDXnTzzLMTwIYx9kupXEvWqIW+2SmrmM15o/hCnx5UJQfMuVcrJ9JTKxJFXbVbWbNeL035eGAuOYusfJQIAMTl9uh9YyBEoyTKuuJxV+fliZaiToVUoRXsHWUaRapTbd/OnG6PZYKPMnuqaubEcez/UwCSmbVamYP3KM/LTBJjlLKm7757+u1HpUkSM6LeyD5NKRkyhKZj8wL3HwYk'
}


def decode_templates():
    out = {}
    for level, s in _TEMPLATE_DATA.items():
        raw = zlib.decompress(base64.b64decode(s))
        out[level] = np.frombuffer(raw, dtype=np.uint8).reshape(TEMPLATE_H, TEMPLATE_W)
    return out


TEMPLATES = decode_templates()


def level_scaled_roi(roi, width, height):
    x1,y1,x2,y2 = roi
    return (
        int(round(x1 * width / REF_W)), int(round(y1 * height / REF_H)),
        int(round(x2 * width / REF_W)), int(round(y2 * height / REF_H)),
    )


def prep(gray):
    # Binary bright glyph mask. This removes most background variation.
    _, bw = cv2.threshold(gray, 120, 255, cv2.THRESH_BINARY)
    return bw


TEMPLATE_MASKS = {lv: prep(img) for lv,img in TEMPLATES.items()}


def similarity(a, b):
    # Dice similarity of foreground pixels.
    # This is the same function used by the proven LEVEL v2_1 recognizer.
    aa = a > 0
    bb = b > 0
    inter = np.count_nonzero(aa & bb)
    denom = np.count_nonzero(aa) + np.count_nonzero(bb)
    return (2.0 * inter / denom) if denom else 0.0

def scale_rect_metadata(rect, width, height):
    x1, y1, x2, y2 = rect
    return (
        int(round(x1 * width / 1920.0)),
        int(round(y1 * height / 1080.0)),
        int(round(x2 * width / 1920.0)),
        int(round(y2 * height / 1080.0)),
    )


def hue_circular_distance(a, b):
    d = abs(float(a) - float(b))
    return min(d, 180.0 - d)


def detect_difficulty_layout(frame_bgr, layout):
    h, w = frame_bgr.shape[:2]
    x1, y1, x2, y2 = scale_rect_metadata(
        DIFFICULTY_ROI_BY_LAYOUT[layout], w, h
    )
    roi = frame_bgr[y1:y2, x1:x2]
    if roi.size == 0:
        return "UNKNOWN", None, 0, {}

    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    H, S, V = cv2.split(hsv)
    valid = (S >= DIFF_MIN_S) & (V >= DIFF_MIN_V)
    count = int(np.count_nonzero(valid))
    if count < DIFF_MIN_COLOR_PIXELS:
        return "UNKNOWN", None, count, {}

    hist = cv2.calcHist(
        [H], [0], valid.astype(np.uint8), [180], [0, 180]
    ).ravel()
    hue_mode = int(np.argmax(hist))
    distances = {
        name: hue_circular_distance(hue_mode, center)
        for name, center in HUE_CENTERS.items()
    }
    best = min(distances, key=distances.get)
    if distances[best] > DIFF_MAX_HUE_DISTANCE:
        best = "UNKNOWN"
    return best, hue_mode, count, distances


def detect_level_layout(frame_bgr, layout):
    h, w = frame_bgr.shape[:2]
    x1, y1, x2, y2 = scale_rect_metadata(
        LEVEL_ROI_BY_LAYOUT[layout], w, h
    )
    crop = frame_bgr[y1:y2, x1:x2]
    if crop.size == 0:
        return None, 0.0, 0.0, {}

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, (TEMPLATE_W, TEMPLATE_H), interpolation=cv2.INTER_AREA)
    mask = prep(gray)

    scores = {}
    for lv, tmpl in TEMPLATE_MASKS.items():
        best = 0.0
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                shifted = np.zeros_like(mask)
                sy1 = max(0, -dy); sy2 = min(TEMPLATE_H, TEMPLATE_H - dy)
                sx1 = max(0, -dx); sx2 = min(TEMPLATE_W, TEMPLATE_W - dx)
                dy1 = max(0, dy); dy2 = dy1 + (sy2 - sy1)
                dx1 = max(0, dx); dx2 = dx1 + (sx2 - sx1)
                shifted[dy1:dy2, dx1:dx2] = mask[sy1:sy2, sx1:sx2]
                best = max(best, similarity(shifted, tmpl))
        scores[lv] = best

    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_lv, best_score = ordered[0]
    second_score = ordered[1][1] if len(ordered) > 1 else 0.0
    margin = best_score - second_score
    result = (
        best_lv
        if best_score >= LEVEL_MIN_SCORE and margin >= LEVEL_MIN_MARGIN
        else None
    )
    return result, best_score, margin, scores


# ------------------------------------------------------------------
# OPTION timing detector
# Based on iidx_option_detector_test_v1_6_prefix_debug.py.
# Four PRE_PLAY layouts:
#   1P / graph-left  : anchor top-left ~= (551, 873)  [existing proven layout]
#   1P / graph-right : anchor top-left ~= (1512, 873) [8.png]
#   2P / graph-left  : anchor top-left ~= (10, 873)   [9.png]
#   2P / graph-right : anchor top-left ~= (971, 873)  [10.png]
#
# SIDE is locked first. Therefore only the two graph-layout candidates for
# that SIDE are searched; no full-screen anchor scan is needed.
# ------------------------------------------------------------------
REF_W = 1920
REF_H = 1080
MAX_LOOP_FPS = 60.0
FRAME_PERIOD = 1.0 / MAX_LOOP_FPS

OPTION_TEMPLATE_FILES = {
    "ANCHOR": "OP_CURRENT_SCORE.png",
    "MIRROR": "Op.MIRROR.png",
    "RANDOM": "Op.RANDOM.png",
    "R-": "Op.R-.png",
    "S-": "Op.S-.png",
}

ANCHOR_EXPECTED = {
    "1P_GRAPH_LEFT":  (551, 873),
    "1P_GRAPH_RIGHT": (1512, 873),
    "2P_GRAPH_LEFT":  (10, 873),
    "2P_GRAPH_RIGHT": (971, 873),
}
ANCHOR_MARGIN_X = 36
ANCHOR_MARGIN_Y = 20
ANCHOR_THRESHOLD = 0.88
OPTION_THRESHOLD = 0.82
PREFIX_THRESHOLD = 0.60
PREFIX_MARGIN = 0.05
OPTION_REL = (-5, 38, 205, 72)

# For timing measurement only. We deliberately do NOT wait after PRE_PLAY.
# The goal is to observe when each signal becomes usable.
OPTION_CONFIRM_FRAMES = 3


def pace_60(loop_start):
    remain = FRAME_PERIOD - (time.perf_counter() - loop_start)
    if remain > 0:
        time.sleep(remain)


def scale_rect_option(rect, w, h):
    x1, y1, x2, y2 = rect
    sx, sy = w / REF_W, h / REF_H
    return (
        int(round(x1 * sx)),
        int(round(y1 * sy)),
        int(round(x2 * sx)),
        int(round(y2 * sy)),
    )


def best_match_option(search_gray, templ_gray):
    sh, sw = search_gray.shape[:2]
    th, tw = templ_gray.shape[:2]
    if sw < tw or sh < th or sw <= 0 or sh <= 0:
        return -1.0, (0, 0)
    res = cv2.matchTemplate(search_gray, templ_gray, cv2.TM_CCOEFF_NORMED)
    _, maxv, _, maxloc = cv2.minMaxLoc(res)
    maxv = float(maxv)
    if not np.isfinite(maxv):
        return -1.0, (0, 0)
    return maxv, maxloc



def anchor_search_roi(layout, anchor_w, anchor_h):
    ex, ey = ANCHOR_EXPECTED[layout]
    return (
        max(0, ex - ANCHOR_MARGIN_X),
        max(0, ey - ANCHOR_MARGIN_Y),
        min(REF_W, ex + anchor_w + ANCHOR_MARGIN_X),
        min(REF_H, ey + anchor_h + ANCHOR_MARGIN_Y),
    )


def candidate_layouts_for_side(side):
    if side == "1P":
        return ("1P_GRAPH_LEFT", "1P_GRAPH_RIGHT")
    return ("2P_GRAPH_LEFT", "2P_GRAPH_RIGHT")


def detect_layout_anchor(bgr, side, anchor_template):
    """Search only the two legal graph positions for the already-locked SIDE."""
    h, w = bgr.shape[:2]
    ah, aw = anchor_template.shape[:2]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    scored = []
    for layout in candidate_layouts_for_side(side):
        roi_ref = anchor_search_roi(layout, aw, ah)
        x1, y1, x2, y2 = scale_rect_option(roi_ref, w, h)
        search = gray[y1:y2, x1:x2]
        score, loc = best_match_option(search, anchor_template)
        abs_pos = (x1 + loc[0], y1 + loc[1])
        scored.append((score, layout, abs_pos))

    scored.sort(reverse=True, key=lambda x: x[0])
    best_score, best_layout, best_pos = scored[0]
    return best_score, best_layout, best_pos, scored


def classify_option(option_gray, option_templates):
    random_score, random_loc = best_match_option(
        option_gray, option_templates["RANDOM"]
    )
    mirror_score, _ = best_match_option(
        option_gray, option_templates["MIRROR"]
    )

    debug = {
        "r_score": -1.0,
        "s_score": -1.0,
        "prefix": "NONE",
    }

    if random_score >= OPTION_THRESHOLD:
        rh, rw = option_templates["RANDOM"].shape[:2]
        ph = max(
            option_templates["R-"].shape[0],
            option_templates["S-"].shape[0],
        )
        pw = max(
            option_templates["R-"].shape[1],
            option_templates["S-"].shape[1],
        )
        rx, ry = random_loc
        px2 = rx
        px1 = max(0, px2 - pw)
        py1 = max(0, ry + (rh - ph) // 2)
        py2 = min(option_gray.shape[0], py1 + ph)
        prefix_roi = option_gray[py1:py2, px1:px2]

        r_score, _ = best_match_option(prefix_roi, option_templates["R-"])
        s_score, _ = best_match_option(prefix_roi, option_templates["S-"])
        debug["r_score"] = r_score
        debug["s_score"] = s_score

        if (
            r_score >= PREFIX_THRESHOLD
            and r_score - s_score >= PREFIX_MARGIN
        ):
            debug["prefix"] = "R-"
            return "R-RANDOM", random_score, mirror_score, debug

        if (
            s_score >= PREFIX_THRESHOLD
            and s_score - r_score >= PREFIX_MARGIN
        ):
            debug["prefix"] = "S-"
            return "S-RANDOM", random_score, mirror_score, debug

        return "RANDOM", random_score, mirror_score, debug

    if mirror_score >= OPTION_THRESHOLD:
        return "MIRROR", random_score, mirror_score, debug

    return "UNKNOWN", random_score, mirror_score, debug


def load_option_templates(templates_dir):
    out = {}
    for key, filename in OPTION_TEMPLATE_FILES.items():
        img = cv_imread_unicode(templates_dir / filename, cv2.IMREAD_GRAYSCALE)
        if img is None or img.size == 0:
            raise OSError(f"Could not load OPTION template: {filename}")
        out[key] = img
    return out



def save_png_unicode(path, image):
    """Unicode-safe PNG save for Japanese Windows paths."""
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        return False
    try:
        encoded.tofile(str(path))
        return True
    except Exception:
        return False


def get_title_roi(frame_bgr, layout):
    h, w = frame_bgr.shape[:2]
    x1, y1, x2, y2 = scaled_roi(TITLE_ROI_BY_LAYOUT[layout], w, h)
    if x2 <= x1 or y2 <= y1:
        return None, (x1, y1, x2, y2)
    return frame_bgr[y1:y2, x1:x2].copy(), (x1, y1, x2, y2)


# ------------------------------------------------------------------
# Song identification method 1: Tesseract + SQLite.
# The ID is independent of OCR strings. Unknown OCR NEVER inserts a song.
# ------------------------------------------------------------------

import itertools
from iidx_normal_chart_recorder import NormalChartRecorder
from iidx_display_outputs import DirectRandomOverlay, load_direct_overlay_config, write_overlay_state
from iidx_monitor import offset
from iidx_display_settings import load_settings, settings_path, history_visible, history_visibility, default_settings
from iidx_history_overlay import DirectHistoryOverlay
from iidx_play_end_detector import EndDetector

GET_READY_SCALES = (1.04,)

# Empirically stable 1P GET READY position is around top-left (94, 525).
# Search a deliberately generous local ROI around it.
GET_READY_SEARCH_X1 = 40
GET_READY_SEARCH_Y1 = 470
GET_READY_SEARCH_X2 = 560
GET_READY_SEARCH_Y2 = 620
GET_READY_APPEAR_SCORE = 0.62
GET_READY_DISAPPEAR_SCORE = 0.52
GET_READY_APPEAR_FRAMES = 3
GET_READY_DISAPPEAR_FRAMES = 3

GR_WAITING = 0
GR_VISIBLE = 1
GR_PLAY_STARTED = 2



LANE_BOUNDARIES_RATIO = [
    0.000, 0.153, 0.282, 0.436, 0.564, 0.718, 0.847, 1.000
]

BLUE_HSV_LOW = (85, 80, 80)
BLUE_HSV_HIGH = (130, 255, 255)
WHITE_HSV_LOW = (0, 0, 145)
WHITE_HSV_HIGH = (179, 80, 255)
ORANGE_HSV_LOW = (3, 90, 70)
ORANGE_HSV_HIGH = (30, 255, 255)
PURPLE_HSV_LOW = (130, 70, 60)
PURPLE_HSV_HIGH = (175, 255, 255)

MIN_ACTIVE_PIXELS = 8
MIN_ACTIVE_RATIO = 0.035
WHITE_LANE_DETECTION_MARGIN_RATIO = 0.15
BLACK_LANE_DETECTION_MARGIN_RATIO = 0.08

MORPH_KERNEL = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 1))




def preprocess_get_ready(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return cv2.GaussianBlur(gray, (3, 3), 0)

def build_get_ready_templates(image):
    base = preprocess_get_ready(image)
    templates = []

    for scale in GET_READY_SCALES:
        width = max(1, int(round(base.shape[1] * scale)))
        height = max(1, int(round(base.shape[0] * scale)))
        interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
        resized = cv2.resize(
            base, (width, height), interpolation=interpolation
        )
        templates.append((scale, resized))

    return templates

def find_best_get_ready_match(frame, templates, side="1P"):
    # Fast realtime search:
    # restrict matching to a small fixed ROI around the proven 1P GET READY
    # position instead of scanning the entire left half of 1920x1080.
    frame_h, frame_w = frame.shape[:2]

    if side == "2P":
        # Mirror only the search AREA, not the glyph template or key numbering.
        sx1 = frame_w - GET_READY_SEARCH_X2 * frame_w / 1920
        sx2 = frame_w - GET_READY_SEARCH_X1 * frame_w / 1920
    else:
        sx1 = GET_READY_SEARCH_X1 * frame_w / 1920
        sx2 = GET_READY_SEARCH_X2 * frame_w / 1920
    x1 = max(0, min(int(sx1), frame_w - 1))
    y1 = max(0, min(int(GET_READY_SEARCH_Y1 * frame_h / 1080), frame_h - 1))
    x2 = max(x1 + 1, min(int(sx2), frame_w))
    y2 = max(y1 + 1, min(int(GET_READY_SEARCH_Y2 * frame_h / 1080), frame_h))

    search_bgr = frame[y1:y2, x1:x2]
    search = preprocess_get_ready(search_bgr)
    search_h, search_w = search.shape[:2]

    best = (-1.0, 0, 0, 0, 0, 1.0)

    for scale, template in templates:
        template_h, template_w = template.shape[:2]

        if template_w > search_w or template_h > search_h:
            continue

        result = cv2.matchTemplate(
            search, template, cv2.TM_CCOEFF_NORMED
        )
        _, score, _, position = cv2.minMaxLoc(result)

        if score > best[0]:
            # Convert ROI-local coordinates back to full-frame coordinates.
            best = (
                float(score),
                int(position[0] + x1),
                int(position[1] + y1),
                int(template_w),
                int(template_h),
                float(scale),
            )

    return best

class RealtimeV5NoteDetector:
    """
    Realtime wrapper around the proven v5 lane detector.

    Detection constants, lane geometry, HSV masks, morphology, per-lane margins,
    thresholds, and rising-edge behavior are intentionally kept unchanged.
    """

    def __init__(self, roi, band):
        self.roi = tuple(map(int, roi))
        self.band = tuple(map(int, band))
        self.last_lane_state = [False] * 7
        self.event_counter = 0

    def reset_detection_session(self):
        self.last_lane_state = [False] * 7
        self.event_counter = 0

    @staticmethod
    def lane_detection_margin_ratio(lane_number):
        if lane_number in (1, 3, 5, 7):
            return WHITE_LANE_DETECTION_MARGIN_RATIO
        return BLACK_LANE_DETECTION_MARGIN_RATIO

    def lane_ranges(self):
        _, _, width, _ = self.roi
        ranges = []
        for lane_index in range(7):
            x1 = round(LANE_BOUNDARIES_RATIO[lane_index] * width)
            x2 = round(LANE_BOUNDARIES_RATIO[lane_index + 1] * width)
            ranges.append((x1, x2))
        return ranges

    @staticmethod
    def build_color_masks(bgr_image):
        hsv = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2HSV)
        ranges = {
            "blue": (BLUE_HSV_LOW, BLUE_HSV_HIGH),
            "white": (WHITE_HSV_LOW, WHITE_HSV_HIGH),
            "orange": (ORANGE_HSV_LOW, ORANGE_HSV_HIGH),
            "purple": (PURPLE_HSV_LOW, PURPLE_HSV_HIGH),
        }

        masks = {}
        for name, (low, high) in ranges.items():
            mask = cv2.inRange(hsv, low, high)
            masks[name] = cv2.morphologyEx(
                mask, cv2.MORPH_OPEN, MORPH_KERNEL
            )

        combined = masks["blue"].copy()
        for name in ("white", "orange", "purple"):
            combined = cv2.bitwise_or(combined, masks[name])

        masks["combined"] = combined
        return masks

    def detect_lanes(self, frame):
        x, y, width, height = self.roi
        band_y1, band_y2 = self.band

        band_y1 = max(0, min(height, band_y1))
        band_y2 = max(0, min(height, band_y2))
        if band_y2 <= band_y1:
            return [False] * 7

        band_image = frame[
            y + band_y1:y + band_y2,
            x:x + width,
        ]
        if band_image.size == 0:
            return [False] * 7

        mask = self.build_color_masks(band_image)["combined"]
        result = []

        for lane_index, (lane_x1, lane_x2) in enumerate(
            self.lane_ranges(), start=1
        ):
            lane_width = lane_x2 - lane_x1
            margin = max(
                2,
                int(
                    lane_width
                    * self.lane_detection_margin_ratio(lane_index)
                ),
            )

            detect_x1 = lane_x1 + margin
            detect_x2 = lane_x2 - margin

            if detect_x2 <= detect_x1:
                result.append(False)
                continue

            lane_mask = mask[:, detect_x1:detect_x2]
            if lane_mask.size == 0:
                result.append(False)
                continue

            active_pixels = cv2.countNonZero(lane_mask)
            area = lane_mask.shape[0] * lane_mask.shape[1]
            active_ratio = active_pixels / max(1, area)

            result.append(
                active_pixels >= MIN_ACTIVE_PIXELS
                and active_ratio >= MIN_ACTIVE_RATIO
            )

        return result

    def register_events(self, lane_state):
        rising_lanes = [
            lane_index + 1
            for lane_index, (old_state, new_state)
            in enumerate(zip(self.last_lane_state, lane_state))
            if new_state and not old_state
        ]

        event_no = None
        if rising_lanes:
            self.event_counter += 1
            event_no = self.event_counter

        self.last_lane_state = list(lane_state)
        return event_no, rising_lanes

LANES = tuple(range(1, 8))

def parse_lanes(text):
    text = str(text).strip()
    if not text:
        return set()
    lanes = {int(x.strip()) for x in text.split("+") if x.strip()}
    if not lanes.issubset(LANES):
        raise ValueError(f"Invalid lanes: {lanes}")
    return lanes

def transform(lanes, permutation):
    return frozenset(permutation[lane - 1] for lane in lanes)

def lanes_text(lanes):
    return "+".join(map(str, sorted(lanes)))

def random_layout_text(permutation):
    return "".join(str(permutation.index(random_lane) + 1) for random_lane in LANES)

class RandomMappingAnalyzer:
    """Validated 5040-permutation elimination from saved-video integrated v3."""
    def __init__(self, normal_events):
        self.normal_events = normal_events
        self.reset()

    def reset(self):
        self.candidates = list(itertools.permutations(LANES))
        self.pair_index = 0
        self.failed = False
        self.confirmed = False
        self.layout = None
        self.permutation = None

    def process(self, random_lanes, random_event_no):
        if self.failed or self.confirmed:
            return
        if self.pair_index >= len(self.normal_events):
            self.failed = True
            print("\nRESULT: RANDOM ANALYSIS FAILED")
            print("NORMAL event sequence ended before RANDOM sequence.")
            return

        normal_event = self.normal_events[self.pair_index]
        normal_lanes = normal_event["lanes"]
        random_lanes = frozenset(random_lanes)
        before = len(self.candidates)

        if len(normal_lanes) != len(random_lanes):
            self.candidates = []
        else:
            self.candidates = [
                p for p in self.candidates
                if transform(normal_lanes, p) == random_lanes
            ]

        self.pair_index += 1
        after = len(self.candidates)
        print(
            f"[RANDOM] pair {self.pair_index:>3}  "
            f"N#{normal_event['event_no']:<3} {lanes_text(normal_lanes):<9} -> "
            f"R#{random_event_no:<3} {lanes_text(random_lanes):<9}  "
            f"candidates {before:>4} -> {after:<4}"
        )

        if after == 0:
            self.failed = True
            print("\n" + "=" * 72)
            print("RESULT: RANDOM ANALYSIS FAILED")
            print(f"First contradiction: pair {self.pair_index}")
            print("Check missed/false detections or NORMAL chart identity.")
            print("=" * 72)
            return

        if after == 1:
            self.permutation = self.candidates[0]
            self.confirmed = True
            self.layout = random_layout_text(self.permutation)
            print("\n" + "=" * 72)
            print("RESULT: RANDOM MAPPING CONFIRMED\n")
            for lane in LANES:
                print(f"  NORMAL {lane} -> RANDOM {self.permutation[lane - 1]}")
            print("\nNORMAL: 1234567")
            print(f"RANDOM: {self.layout}")
            print(f"\nFirst unique point : pair {self.pair_index}")
            print(f"NORMAL event       : #{normal_event['event_no']} ({lanes_text(normal_lanes)})")
            print(f"RANDOM event       : #{random_event_no} ({lanes_text(random_lanes)})")
            print("=" * 72)

DB_FILENAME = "iidx_songs.sqlite3"
TESSERACT_EXE = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")

def open_song_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS songs (
        song_id INTEGER PRIMARY KEY AUTOINCREMENT,
        official_title TEXT NOT NULL UNIQUE,
        ocr_title TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS normal_charts (
        song_id INTEGER NOT NULL,
        difficulty TEXT NOT NULL,
        level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 12),
        note_events TEXT NOT NULL,
        PRIMARY KEY(song_id, difficulty),
        FOREIGN KEY(song_id) REFERENCES songs(song_id)
    );
    """)
    conn.commit()
    return conn

def normalize_ocr(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in text if not ch.isspace())

def match_song(conn, ocr_text):
    needle = normalize_ocr(ocr_text)
    if not needle:
        return None, "EMPTY", []
    rows = conn.execute("SELECT song_id, official_title, ocr_title FROM songs").fetchall()
    scored = sorted(((difflib.SequenceMatcher(None, needle, normalize_ocr(r[2])).ratio(), r)
                     for r in rows), key=lambda x: x[0], reverse=True)
    suggestions = [(round(score, 3), r[0], r[1]) for score, r in scored[:3]]
    if not scored:
        return None, "NO_DB_ENTRIES", suggestions
    best_score, best = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    if best_score == 1.0 and second < 1.0:
        return best, "EXACT", suggestions
    # Conservative: ambiguous results are UNKNOWN, never silently create an ID.
    if best_score >= 0.94 and best_score - second >= 0.10:
        return best, "FUZZY", suggestions
    return None, "UNKNOWN", suggestions

def tesseract_ocr(crop_bgr, lang):
    exe = str(TESSERACT_EXE) if TESSERACT_EXE.is_file() else shutil.which("tesseract")
    if not exe:
        raise FileNotFoundError("Tesseract not found: install or add tesseract.exe to PATH")
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    up = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    ok, encoded = cv2.imencode(".png", up)
    if not ok:
        raise RuntimeError("OCR image encode failed")
    # Prevent the Tesseract child process from creating/activating a console
    # window while INFINITAS has focus. Keep the OCR pipeline unchanged.
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    proc = subprocess.run([exe, "stdin", "stdout", "-l", lang, "--psm", "7", "--oem", "1"],
                          input=encoded.tobytes(), capture_output=True, timeout=10,
                          creationflags=creationflags)
    if proc.returncode:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip())
    return proc.stdout.decode("utf-8", "replace").strip().replace("\n", " ")

def auto_register_song(conn, title):
    """Register a previously unknown OCR title, without replacing any existing song."""
    title = " ".join(title.strip().split())
    if not title or len(title) < 3 or len(title) > 200 or not any(ch.isalnum() for ch in title):
        print(f"[SONG AUTO REGISTER SKIPPED] invalid OCR title={title!r}", flush=True)
        return None
    needle = normalize_ocr(title)
    # Guard against duplicates with different capitalization or spacing.
    rows = conn.execute("SELECT song_id,official_title,ocr_title FROM songs").fetchall()
    for song_id, official, ocr in rows:
        if needle in (normalize_ocr(official), normalize_ocr(ocr)):
            print(f"[SONG IDENTIFIED] song_id={song_id} title={official!r} method=NORMALIZED_EXACT", flush=True)
            return song_id
    # A close but nonmatching existing entry could be an OCR typo. Do not
    # silently create a duplicate in that case; it can be reviewed manually.
    nearest = max((difflib.SequenceMatcher(None, needle, normalize_ocr(r[2])).ratio()
                   for r in rows), default=0.0)
    if nearest >= 0.80:
        print(f"[SONG AUTO REGISTER SKIPPED] possible existing title, similarity={nearest:.3f} OCR={title!r}", flush=True)
        return None
    try:
        cur = conn.execute("INSERT INTO songs(official_title,ocr_title) VALUES(?,?)", (title, title))
        conn.commit()
        print(f"[SONG AUTO REGISTERED] song_id={cur.lastrowid} official_title={title!r} ocr_title={title!r}", flush=True)
        return cur.lastrowid
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        print(f"[SONG AUTO REGISTER SKIPPED] {exc}", flush=True)
        return None


def identify_title(crop, conn):
    started = time.perf_counter()
    outputs = []
    for lang in ("jpn", "eng+jpn"):
        try:
            raw = tesseract_ocr(crop, lang)
            row, status, candidates = match_song(conn, raw)
            outputs.append((lang, raw, status, candidates))
            print(f"[TITLE OCR] lang={lang} text={raw!r} match={status} candidates={candidates}", flush=True)
            if row is not None:
                print(f"[SONG IDENTIFIED] song_id={row[0]} title={row[1]!r} "
                      f"method={lang}/{status} elapsed={time.perf_counter()-started:.3f}s", flush=True)
                return row[0]
        except Exception as exc:
            print(f"[TITLE OCR ERROR] lang={lang} {exc}", flush=True)
    # Prefer the final eng+jpn OCR result; never register the preliminary jpn result.
    final_ocr = next((raw for lang, raw, status, _ in reversed(outputs)
                      if lang == "eng+jpn" and raw.strip()), None)
    if final_ocr is None:
        print(f"[SONG UNKNOWN] elapsed={time.perf_counter()-started:.3f}s; no usable final OCR", flush=True)
        return None
    return auto_register_song(conn, final_ocr)

def title_ocr_worker(crop, db_path, generation, results):
    """OCR and DB matching run off the capture thread; DB connection is thread-local."""
    worker_conn = None
    try:
        worker_conn = sqlite3.connect(str(db_path))
        song_id = identify_title(crop, worker_conn)
        results.put((generation, song_id, None))
    except Exception as exc:
        results.put((generation, None, str(exc)))
    finally:
        if worker_conn is not None:
            worker_conn.close()


def read_normal_events(csv_path):
    """Read v5 events_debug CSV without modifying original detector output."""
    events = []
    with open(csv_path, 'r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        required = {'event_no', 'event_lanes'}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"CSV missing columns: {sorted(required - set(reader.fieldnames or []))}")
        for row in reader:
            no = int(row['event_no'])
            lanes_raw = row['event_lanes'].strip()
            # Preserve simultaneous-note events as a single event with multiple lanes.
            import re
            lanes = [int(x) for x in re.findall(r'[1-7]', lanes_raw)]
            if not lanes or len(lanes) != len(set(lanes)):
                raise ValueError(f"Invalid event_lanes at event {no}: {lanes_raw!r}")
            if no != len(events) + 1:
                raise ValueError(f"event_no must be sequential from 1: got {no}")
            events.append({'event_no': no, 'event_lanes': lanes})
    if not events:
        raise ValueError('CSV contains no events')
    return events


def chart_db_command(args, conn):
    if args.add_chart:
        official, difficulty, level_text, csv_name = args.add_chart
        difficulty = difficulty.upper()
        if difficulty not in ('BEGINNER', 'NORMAL', 'HYPER', 'ANOTHER', 'LEGGENDARIA'):
            raise ValueError('difficulty must be BEGINNER/NORMAL/HYPER/ANOTHER/LEGGENDARIA')
        level = int(level_text)
        if not 1 <= level <= 12:
            raise ValueError('level must be 1..12')
        row = conn.execute('SELECT song_id FROM songs WHERE official_title=?', (official,)).fetchone()
        if row is None:
            print(f'[CHART IMPORT ERROR] song not registered: {official!r}; use --add-song first')
            return 1
        events = read_normal_events(Path(csv_name))
        exists = conn.execute('SELECT 1 FROM normal_charts WHERE song_id=? AND difficulty=?',
                              (row[0], difficulty)).fetchone()
        if exists:
            print('[CHART ALREADY EXISTS] refusing to overwrite existing chart')
            return 1
        conn.execute('INSERT INTO normal_charts(song_id,difficulty,level,note_events) VALUES(?,?,?,?)',
                     (row[0], difficulty, level, json.dumps(events, ensure_ascii=False)))
        conn.commit()
        print(f'[CHART REGISTERED] song_id={row[0]} title={official!r} difficulty={difficulty} '
              f'level={level} events={len(events)}')
        return 0
    if args.list_charts:
        for song_id, title, difficulty, level, raw in conn.execute("SELECT c.song_id,s.official_title,c.difficulty,c.level,c.note_events FROM normal_charts c JOIN songs s ON s.song_id=c.song_id ORDER BY c.song_id,c.difficulty"):
            print(f'[CHART] song_id={song_id} title={title!r} difficulty={difficulty} '
                  f'level={level} events={len(json.loads(raw))}')
        return 0
def db_command(args, conn):
    if args.add_song:
        official, ocr = args.add_song
        existing = conn.execute("SELECT song_id FROM songs WHERE official_title=?", (official,)).fetchone()
        if existing:
            print(f"[ALREADY EXISTS] song_id={existing[0]} title={official!r}; no changes")
            return 0
        conn.execute("INSERT INTO songs(official_title,ocr_title) VALUES(?,?)", (official, ocr))
        conn.commit()
        print(f"[SONG REGISTERED] song_id={conn.execute('SELECT last_insert_rowid()').fetchone()[0]} title={official!r}")
        return 0
    if args.set_song_titles:
        song_id_text, official, ocr = args.set_song_titles
        song_id = int(song_id_text)
        row = conn.execute("SELECT song_id FROM songs WHERE song_id=?", (song_id,)).fetchone()
        if row is None:
            print(f"[SONG TITLE UPDATE ERROR] song_id={song_id} not found")
            return 1
        try:
            conn.execute("UPDATE songs SET official_title=?, ocr_title=? WHERE song_id=?",
                         (official, ocr, song_id))
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            print(f"[SONG TITLE UPDATE ERROR] {exc}")
            return 1
        print(f"[SONG TITLES UPDATED] song_id={song_id} official_title={official!r} ocr_title={ocr!r}")
        return 0
    if args.list_songs:
        for song_id, official, ocr in conn.execute("SELECT song_id,official_title,ocr_title FROM songs ORDER BY song_id"):
            print(f"{song_id:5d} | {official} | OCR={ocr}")
        return 0
    return None

def _replace_state_file_best_effort(tmp, target):
    """OBS state output must never terminate live analysis on Windows file locks."""
    for attempt in range(3):
        try:
            tmp.replace(target)
            return True
        except OSError as exc:
            if attempt == 2:
                print(f"[OVERLAY STATE WARNING] update skipped: {target.name}: {exc}", flush=True)
                return False
            time.sleep(0.015)


def publish_display_state(js_path, json_path, confirmed=False, layout="", *, history=None, phase=None, settings=None, side="1P"):
    """Publish matching JSON and legacy OBS JS using atomic file replacement."""
    layout = str(layout) if confirmed else ""
    if confirmed and (len(layout) != 7 or set(layout) != set("1234567")):
        raise ValueError(f"Invalid confirmed layout: {layout!r}")
    payload = {"confirmed": bool(confirmed), "layout": layout}
    if history is not None:
        payload["history"] = history
    if phase is not None:
        payload["phase"] = phase
    if settings is not None:
        payload["history_visible"] = history_visible(settings, phase, side)
        payload["history_visibility"] = history_visibility(settings, side)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = json_path.with_suffix(json_path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"[OVERLAY STATE WARNING] JSON write failed: {exc}", flush=True)
    else:
        _replace_state_file_best_effort(tmp, json_path)
    # Publish the full state for OBS while preserving confirmed/layout keys.
    js_tmp = js_path.with_suffix(js_path.suffix + ".tmp")
    try:
        js_tmp.write_text("window.IIDX_RANDOM_STATE = " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n", encoding="utf-8")
    except OSError as exc:
        print(f"[OVERLAY STATE WARNING] JS write failed: {exc}", flush=True)
    else:
        _replace_state_file_best_effort(js_tmp, js_path)


def main():
    parser = argparse.ArgumentParser(description="IIDX v1_4_2 auto song registration + NORMAL chart recorder + RANDOM DB realtime")
    parser.add_argument("--add-song", nargs=2, metavar=("OFFICIAL_TITLE", "OCR_TITLE"))
    parser.add_argument("--list-songs", action="store_true")
    parser.add_argument("--overlay-state", type=Path, default=Path("web/iidx_random_state.js"))
    parser.add_argument("--overlay-json", type=Path, default=Path("web/iidx_random_state.json"))
    parser.add_argument("--direct-overlay-config", type=Path, default=Path("config/iidx_overlay_config.json"))
    parser.add_argument("--set-song-titles", nargs=3,
                        metavar=("SONG_ID", "OFFICIAL_TITLE", "OCR_TITLE"),
                        help="Update song titles without changing song_id or charts")
    parser.add_argument("--add-chart", nargs=4,
                        metavar=("OFFICIAL_TITLE", "DIFFICULTY", "LEVEL", "CSV_PATH"))
    parser.add_argument("--list-charts", action="store_true")
    parser.add_argument('--get-ready-template', type=Path, default=Path('templates/GET_READY.png'))
    parser.add_argument('--roi-json', type=Path, default=Path('config/roi_default.json'))
    parser.add_argument("--launcher-mode", action="store_true", help="Use launcher-controlled shutdown; disable ESC/Q quit")
    args = parser.parse_args()
    launcher_stop = threading.Event()
    if args.launcher_mode:
        def _listen_launcher_stop():
            for command in sys.stdin:
                if command.strip().upper() == "STOP":
                    launcher_stop.set()
                    break
        threading.Thread(target=_listen_launcher_stop, daemon=True, name="launcher-stop-listener").start()
    from iidx_paths import app_root
    base = app_root()
    templates_dir = base / "templates"
    data_dir = base / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = open_song_db(data_dir / DB_FILENAME)
    try:
        chart_result = chart_db_command(args, conn)
    except (OSError, ValueError, csv.Error) as exc:
        print(f'[CHART IMPORT ERROR] {exc}', flush=True)
        conn.close()
        return 1
    if chart_result is not None:
        conn.close()
        return chart_result
    command_result = db_command(args, conn)
    if command_result is not None:
        conn.close()
        return command_result
    # The 1P GET READY search and v5 detector are frozen from the validated v1.3.
    get_ready_path = args.get_ready_template
    if not get_ready_path.is_absolute():
        get_ready_path = base / get_ready_path
    roi_path = args.roi_json
    if not roi_path.is_absolute():
        roi_path = base / roi_path
    if not get_ready_path.is_file() or not roi_path.is_file():
        print(f'[RANDOM SETUP ERROR] GET_READY={get_ready_path.is_file()} ROI_JSON={roi_path.is_file()}')
        conn.close()
        return 2
    try:
        # Unicode-safe loading: Windows paths containing Japanese characters.
        template_image = cv_imread_unicode(get_ready_path, cv2.IMREAD_COLOR)
    except (OSError, ValueError) as exc:
        print(f'[RANDOM SETUP ERROR] GET_READY image unreadable: {exc}')
        conn.close()
        return 2
    get_ready_templates = build_get_ready_templates(template_image)
    roi_data = json.loads(roi_path.read_text(encoding='utf-8'))
    base_roi = tuple(map(int, roi_data['roi']))
    base_band = tuple(map(int, roi_data['band_y_in_roi']))
    rx, ry, rw, rh = base_roi
    band_top = base_band[0]
    band_bottom = band_top + 20
    roi = (rx, ry, rw, max(rh, band_bottom))
    band = (band_top, band_bottom)
    note_detector = RealtimeV5NoteDetector(roi, band)
    # Separate 2P detector: the horizontal position is provisionally mirrored.
    # Keep the proven 1P detector and config completely unchanged.
    editor_2p = roi_data.get('editor_2p', {})
    if not isinstance(editor_2p, dict):
        editor_2p = {}
    roi2_source = editor_2p.get('roi', [1920 - rx - rw, ry, rw, rh])
    band2_source = editor_2p.get('band_y_in_roi', list(base_band))
    if len(roi2_source) != 4 or len(band2_source) != 2:
        raise ValueError('editor_2p ROI/band format invalid')
    r2x, r2y, r2w, r2h = map(int, roi2_source)
    b2top = int(band2_source[0])
    if not (0 <= r2x < 1920 and 0 <= r2y < 1080 and r2w > 0 and r2h > 0
            and r2x + r2w <= 1920 and 0 <= b2top and r2y + b2top + 20 <= 1080):
        raise ValueError('editor_2p ROI/band is outside 1920x1080')
    roi2 = (r2x, r2y, r2w, max(r2h, b2top + 20))
    band2 = (b2top, b2top + 20)
    note_detector_2p = RealtimeV5NoteDetector(roi2, band2)
    print(f'[2P DETECTION CONFIG] ROI={roi2} band={band2} (horizontal provisional)', flush=True)
    random_analyzer = None
    normal_recorder = None
    chart_events = None
    chart_ready = False
    gr_state = GR_WAITING
    gr_appear_count = 0
    gr_disappear_count = 0
    play_active = False
    print(f'[RANDOM DB MODE] ROI={roi} band={band} GET_READY={get_ready_path.name}')
    path01 = templates_dir / TEMPLATE_01_SIDE
    path02 = templates_dir / TEMPLATE_02_SIDE
    path_no = templates_dir / TEMPLATE_NO_ENTRY
    path_scratch = templates_dir / TEMPLATE_SCRATCH
    option_templates = None

    print("=" * 96)
    print("IIDX SIDE -> PRE_PLAY -> 4-LAYOUT -> OPTION + DIFFICULTY + LEVEL test v1_4")
    print("=" * 96)
    print("Sender        :", SENDER_NAME)
    print("SIDE templates:", path01.name, "/", path02.name, "/", path_no.name)
    print("PRE_PLAY tmpl :", path_scratch.name)
    print(f"SIDE confirm  : valid PLAYER/NO ENTRY combination x {SIDE_CONFIRM_FRAMES} -> LOCK")
    print("1P condition  : LEFT=PLAYER 01 SIDE + RIGHT=NO ENTRY")
    print("2P condition  : LEFT=NO ENTRY + RIGHT=PLAYER 02 SIDE")
    print("After SIDE LOCK: SIDE matching stops, then only that side's scratch ROI is monitored")
    print(f"PRE_PLAY 1P   : threshold={SCRATCH_THRESHOLD['1P']:.2f} expected={SCRATCH_EXPECTED_POS['1P']}")
    print(f"PRE_PLAY 2P   : threshold={SCRATCH_THRESHOLD['2P']:.2f} expected={SCRATCH_EXPECTED_POS['2P']}")
    print(f"PRE_PLAY confirm: x{SCRATCH_CONFIRM_FRAMES} consecutive frames")
    print("OPTION timing : AUTO after PRE_PLAY; no artificial wait")
    print("LAYOUT detect : 1P/2P x GRAPH_LEFT/GRAPH_RIGHT (2 candidates after SIDE lock)")
    print("DIFFICULTY    : HSV color detector after layout x3")
    print("LEVEL         : dedicated LV.1-LV.12 recognizer after layout x3")
    print(f"OPTION prefix : threshold={PREFIX_THRESHOLD:.2f} margin={PREFIX_MARGIN:.2f}")
    print(f"Main loop cap : {MAX_LOOP_FPS:.0f} fps (non-PLAYING policy)")
    print("Debug preview : OFF")
    print("OPTION NORMAL : anchor valid + no RANDOM/MIRROR = NORMAL (OFF/LEGACY)")
    print("DB WRITES     : auto-register NORMAL chart only when uniquely identifiable")
    print("Q / ESC       : quit")
    print()

    try:
        t01, m01 = load_template(path01)
        t02, m02 = load_template(path02)
        tno, mno = load_template(path_no)
        ts, ms = load_scratch_template(path_scratch)
        option_templates = load_option_templates(templates_dir)
    except Exception as e:
        print("[TEMPLATE LOAD FAILED]", repr(e))
        print("Place PLAYER_01_SIDE.png, PLAYER_02_SIDE.png, NO_ENTRY.png,")
        print("and SCRATCH.png plus OPTION templates in the templates/ folder.")
        return 2

    sh, sw = ts.shape
    print(f"[TEMPLATE] P01={t01.shape[1]}x{t01.shape[0]}  P02={t02.shape[1]}x{t02.shape[0]}  "
          f"NO={tno.shape[1]}x{tno.shape[0]}  SCRATCH={sw}x{sh}")

    # OpenGL context only. The black pygame window is intentionally tiny;
    # hiding it completely is a separate future validation task.
    pygame.init()
    pygame.display.set_caption("SpoutGL context - SONG OCR ASYNC v1_1")
    pygame.display.set_mode((64, 64), DOUBLEBUF | OPENGL)
    texture_id = glGenTextures(1)
    allocate_texture(texture_id, INITIAL_W, INITIAL_H)

    last_size = None
    last_connected = None
    interval_frames = 0
    report_t0 = time.perf_counter()

    locked_side = None
    side_candidate = None
    side_candidate_count = 0
    return_side_candidate = None
    return_side_count = 0

    pre_candidate_count = 0
    pre_play_detected = False
    latest_scratch_score = -1.0
    latest_scratch_pos = None

    # OPTION timing state, reset at every new PRE_PLAY detection.
    pre_play_t0 = None
    option_anchor_first_t = None
    option_first_class_t = None
    option_candidate = None
    option_candidate_count = 0
    option_confirmed = None
    option_last_result = "UNKNOWN"
    option_last_anchor = -1.0
    option_last_random = -1.0
    option_last_mirror = -1.0
    option_last_r = -1.0
    option_last_s = -1.0
    detected_layout = None
    layout_candidate = None
    layout_candidate_count = 0
    difficulty_candidate = None
    difficulty_candidate_count = 0
    difficulty_confirmed = None
    difficulty_last = "UNKNOWN"
    difficulty_last_hue = None
    level_candidate = None
    level_candidate_count = 0
    level_confirmed = None
    level_last = None
    level_last_score = 0.0
    level_last_margin = 0.0
    title_roi_saved = False
    title_roi_last_coords = None
    song_ocr_attempted = False
    song_id_confirmed = None
    ocr_results = queue.SimpleQueue()
    ocr_generation = 0

    # Publish to both OBS browser source and in-game direct overlay.
    overlay_state_path = args.overlay_state if args.overlay_state.is_absolute() else base / args.overlay_state
    overlay_json_path = args.overlay_json if args.overlay_json.is_absolute() else base / args.overlay_json
    overlay_config_path = (args.direct_overlay_config if args.direct_overlay_config.is_absolute()
                           else base / args.direct_overlay_config)
    # Unified per-side history visibility; history itself remains session-only.
    display_settings_file = settings_path(base)
    try:
        display_settings = load_settings(display_settings_file)
    except ValueError as exc:
        print(f"[DISPLAY SETTINGS ERROR] {exc}; using defaults", flush=True)
        display_settings = default_settings()
    print(f"[DISPLAY SETTINGS LOADED] {display_settings_file} { {side: history_visibility(display_settings, side) for side in ('1P', '2P')} }", flush=True)
    current_display_phase = "IDLE"
    current_display_confirmed = False
    current_display_layout = ""
    history_overlay = None
    last_settings_check = 0.0
    last_state_signature = None
    last_settings_mtime = display_settings_file.stat().st_mtime_ns if display_settings_file.is_file() else None
    def publish(confirmed=False, layout="", *, phase=None):
        nonlocal current_display_phase, current_display_confirmed, current_display_layout
        old_phase = current_display_phase
        if phase is not None:
            current_display_phase = phase
        if old_phase != current_display_phase:
            print(f"[PHASE CHANGE] {old_phase} -> {current_display_phase} history_visible={history_visible(display_settings, current_display_phase, locked_side or '1P')} entries={len(session_history)}", flush=True)
        current_display_confirmed = bool(confirmed)
        current_display_layout = str(layout) if confirmed else ""
        publish_display_state(overlay_state_path, overlay_json_path, confirmed, layout,
                              history=session_history, phase=current_display_phase, settings=display_settings, side=locked_side or '1P')
        if history_overlay is not None:
            history_overlay.update_history(session_history, history_visible(display_settings, current_display_phase, locked_side or '1P'), current_display_phase)

    # Session-only history. The existing OBS JS contract stays unchanged.
    session_history = []
    history_csv_file = None

    def append_session_history_csv(entry, side):
        """Append one completed play in chronological order; create on first entry."""
        nonlocal history_csv_file
        now = datetime.now()
        columns = ('recorded_at', 'side', 'song_id', 'title', 'difficulty',
                   'level', 'option', 'layout', 'status', 'end_type')
        row = {'recorded_at': now.strftime('%Y-%m-%d %H:%M:%S'),
               'side': side if side in ('1P', '2P') else 'UNKNOWN'}
        row.update({key: entry.get(key) for key in columns if key not in row})
        try:
            if history_csv_file is None:
                folder = base / 'history_csv'
                folder.mkdir(parents=True, exist_ok=True)
                stamp = now.strftime('%Y%m%d_%H%M%S')
                # Exclusive creation prevents overwriting another session's file.
                for suffix in range(1000):
                    name = f'iidx_history_{stamp}' + (f'_{suffix:03d}' if suffix else '') + '.csv'
                    candidate = folder / name
                    try:
                        with candidate.open('x', encoding='utf-8-sig', newline='') as f:
                            writer = csv.DictWriter(f, fieldnames=columns)
                            writer.writeheader()
                            writer.writerow(row)
                        history_csv_file = candidate
                        print(f'[HISTORY CSV CREATED] {candidate}', flush=True)
                        return
                    except FileExistsError:
                        continue
                raise FileExistsError(f'CSV filename collision limit reached: {folder}')
            with history_csv_file.open('a', encoding='utf-8', newline='') as f:
                csv.DictWriter(f, fieldnames=columns).writerow(row)
            print(f'[HISTORY CSV APPENDED] {history_csv_file}', flush=True)
        except OSError as exc:
            # CSV failure must never stop live gameplay or history display.
            print(f'[HISTORY CSV ERROR] {type(exc).__name__}: {exc}', flush=True)

    play_end_detector = None
    try:
        play_end_detector = EndDetector(templates_dir, threshold=0.72)
        print("[PLAY END READY] BRIDGE=Bridge_to_Result.png FAILED=Failed_to_Bridge.png; match every 2 frames", flush=True)
    except Exception as exc:
        print(f"[PLAY END DISABLED] {exc}", flush=True)
    end_frame_count = 0
    current_play_ended = False
    play_started_for_end = False
    publish(False, "", phase="IDLE")
    direct_overlay = None
    direct_profiles = {}
    applied_direct_side = None
    try:
        overlay_x, overlay_y, overlay_scale = load_direct_overlay_config(overlay_config_path)
        # Issue 10: independent GUI controls direct RANDOM overlay visibility.
        # Missing 'enabled' preserves previous behavior (visible).
        direct_enabled = True
        try:
            _direct_cfg = json.loads(overlay_config_path.read_text(encoding="utf-8-sig"))
            direct_enabled = _direct_cfg.get("enabled", True)
            if not isinstance(direct_enabled, bool):
                raise ValueError("enabled must be true or false")
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            print(f"[DIRECT OVERLAY ENABLED] invalid setting: {exc}; default=True", flush=True)
            direct_enabled = True
        _dx1, _dy1 = offset(_direct_cfg.get('monitor', '') if isinstance(locals().get('_direct_cfg'), dict) else '', 'RANDOM 1P')
        direct_profiles['1P'] = (overlay_x + _dx1, overlay_y + _dy1, overlay_scale, direct_enabled)
        _side2 = _direct_cfg.get('editor_2p', {}) if isinstance(locals().get('_direct_cfg'), dict) else {}
        if not isinstance(_side2, dict):
            _side2 = {}
        try:
            _p2x = int(_side2.get('x', overlay_x))
            _p2y = int(_side2.get('y', overlay_y))
            _p2scale = max(0.50, min(2.00, float(_side2.get('scale', overlay_scale))))
            _p2enabled = _side2.get('enabled', direct_enabled)
            if not isinstance(_p2enabled, bool):
                raise ValueError('2P enabled must be a boolean')
            _dx2, _dy2 = offset(_side2.get('monitor', ''), 'RANDOM 2P')
            direct_profiles['2P'] = (_p2x + _dx2, _p2y + _dy2, _p2scale, _p2enabled)
        except (TypeError, ValueError) as exc:
            print(f"[DIRECT OVERLAY 2P CONFIG ERROR] {exc}; fallback to 1P", flush=True)
            direct_profiles['2P'] = direct_profiles['1P']
        print(f"[DIRECT OVERLAY PROFILES] 1P={direct_profiles['1P']} 2P={direct_profiles['2P']}", flush=True)
        direct_overlay = DirectRandomOverlay(*direct_profiles['1P'][:3])
        direct_overlay.enabled = direct_enabled
        def apply_direct_side(side):
            nonlocal applied_direct_side
            if direct_overlay is None or side not in direct_profiles or applied_direct_side == side:
                return
            direct_overlay.apply_profile(*direct_profiles[side])
            applied_direct_side = side
            print(f"[DIRECT OVERLAY SIDE APPLIED] side={side} x={direct_profiles[side][0]} y={direct_profiles[side][1]} scale={direct_profiles[side][2]} enabled={direct_profiles[side][3]}", flush=True)
        print(f"[DIRECT OVERLAY ENABLED] {direct_enabled}", flush=True)
        history_overlay = DirectHistoryOverlay(direct_overlay.root)
        history_overlay.update_history(session_history, history_visible(display_settings, current_display_phase, locked_side or '1P'), current_display_phase)
        print(f"[DISPLAY OUTPUTS READY] OBS={overlay_state_path} DIRECT={overlay_config_path}")
    except Exception as exc:
        print(f"[DIRECT OVERLAY ERROR] {exc}; OBS output remains enabled", flush=True)

    try:
        with SpoutGL.SpoutReceiver() as receiver:
            receiver.setReceiverName(SENDER_NAME)
            print(f"[READY] waiting for {SENDER_NAME} ...")

            while True:
                loop_start = time.perf_counter()
                if loop_start - last_settings_check >= 1.0:
                    last_settings_check = loop_start
                    new_mtime = display_settings_file.stat().st_mtime_ns if display_settings_file.is_file() else None
                    if new_mtime != last_settings_mtime:
                        try:
                            updated_settings = load_settings(display_settings_file)
                        except ValueError as exc:
                            print(f"[DISPLAY SETTINGS RELOAD ERROR] {exc}", flush=True)
                        else:
                            display_settings = updated_settings
                            last_settings_mtime = new_mtime
                            print(f"[DISPLAY SETTINGS RELOADED] { {side: history_visibility(display_settings, side) for side in ('1P', '2P')} }", flush=True)
                            publish(current_display_confirmed, current_display_layout, phase=current_display_phase)
                while True:
                    try:
                        result_generation, result_id, result_error = ocr_results.get_nowait()
                    except queue.Empty:
                        break
                    if result_generation != ocr_generation:
                        print("[TITLE OCR STALE] previous PRE_PLAY result ignored", flush=True)
                        continue
                    if result_error:
                        print(f"[TITLE OCR WORKER ERROR] {result_error}", flush=True)
                    chart_ready = False
                    chart_events = None
                    song_id_confirmed = result_id
                    if song_id_confirmed is not None:
                        chart = conn.execute(
                            "SELECT level FROM normal_charts WHERE song_id=? AND difficulty=?",
                            (song_id_confirmed, difficulty_confirmed)).fetchone()
                        if chart is not None and chart[0] != level_confirmed:
                            print(f"[CHART LEVEL MISMATCH] DB={chart[0]} screen={level_confirmed}; "
                                  "chart use prohibited", flush=True)
                        elif chart is None:
                            print("[CHART NOT REGISTERED] song identified; no normal chart yet", flush=True)
                        else:
                            raw = conn.execute(
                                "SELECT note_events FROM normal_charts WHERE song_id=? AND difficulty=?",
                                (song_id_confirmed, difficulty_confirmed)).fetchone()[0]
                            events = json.loads(raw)
                            print(f"[CHART FOUND] song_id={song_id_confirmed} difficulty={difficulty_confirmed} "
                                  f"level={chart[0]} events={len(events)} verified", flush=True)
                            chart_events = [
                                {'event_no': int(e['event_no']),
                                 'lanes': frozenset(int(lane) for lane in e['event_lanes'])}
                                for e in events
                            ]
                            if (chart_events and all(chart_events[i]['event_no'] == i + 1
                                                     for i in range(len(chart_events)))):
                                chart_ready = True
                                print(f'[RANDOM CHART READY] song_id={song_id_confirmed} events={len(chart_events)}')
                            else:
                                chart_events = None
                                print('[RANDOM CHART ERROR] invalid event sequence')
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        return 0
                    if not args.launcher_mode and event.type == pygame.KEYDOWN and event.key in (pygame.K_q, pygame.K_ESCAPE):
                        return 0

                ok = receiver.receiveTexture(texture_id, GL_TEXTURE_2D, False, 0)
                connected = bool(receiver.isConnected())
                is_new = bool(receiver.isFrameNew())
                is_updated = bool(receiver.isUpdated())

                if connected != last_connected:
                    print("[CONNECTION]", "CONNECTED" if connected else "DISCONNECTED")
                    last_connected = connected
                    if not connected:
                        last_size = None

                if connected:
                    width = int(receiver.getSenderWidth())
                    height = int(receiver.getSenderHeight())

                    if width > 0 and height > 0:
                        if is_updated or last_size != (width, height):
                            allocate_texture(texture_id, width, height)
                            last_size = (width, height)
                            print(f"[TEXTURE UPDATE] updated={is_updated} size={width}x{height}")
                            ok = receiver.receiveTexture(texture_id, GL_TEXTURE_2D, False, 0)
                            connected = bool(receiver.isConnected())
                            is_new = bool(receiver.isFrameNew())
                            is_updated = bool(receiver.isUpdated())

                        if ok and connected:
                            glBindTexture(GL_TEXTURE_2D, texture_id)
                            pixels = glGetTexImage(GL_TEXTURE_2D, 0, GL_RGBA, GL_UNSIGNED_BYTE)
                            arr = np.frombuffer(pixels, dtype=np.uint8)

                            if arr.size == width * height * 4:
                                rgba = arr.reshape((height, width, 4))
                                bgr = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)

                                # Play-end detection: only while a play is armed; once per play.
                                if play_started_for_end and not current_play_ended and play_end_detector is not None:
                                    end_frame_count += 1
                                    if end_frame_count % 2 == 0:
                                        play_end_detector.check(bgr)
                                        if play_end_detector.end_processed:
                                            current_play_ended = True
                                            play_started_for_end = False
                                            play_active = False
                                            confirmed_layout = (random_analyzer.layout
                                                                if random_analyzer is not None and random_analyzer.confirmed
                                                                else None)
                                            if option_confirmed in ('RANDOM', 'R-RANDOM', 'MIRROR'):
                                                title_row = (conn.execute('SELECT official_title FROM songs WHERE song_id=?',
                                                                          (song_id_confirmed,)).fetchone()
                                                             if song_id_confirmed is not None else None)
                                                entry = {
                                                    'song_id': song_id_confirmed,
                                                    'title': title_row[0] if title_row else None,
                                                    'difficulty': difficulty_confirmed,
                                                    'level': level_confirmed,
                                                    'option': option_confirmed,
                                                    'layout': confirmed_layout,
                                                    'status': 'CONFIRMED' if confirmed_layout else 'UNCONFIRMED',
                                                    'end_type': ('FAILED' if play_end_detector.counts['FAILED'] >= 3 else 'BRIDGE'),
                                                }
                                                session_history.insert(0, entry)
                                                append_session_history_csv(entry, locked_side)
                                                print(f'[HISTORY ADDED] {entry}', flush=True)
                                            return_side_candidate = None
                                            return_side_count = 0
                                            publish(False, "", phase="ENDED")
                                            if direct_overlay is not None:
                                                direct_overlay.clear()
                                            print('[MAIN OVERLAY CLEARED] play ended', flush=True)

                                # GET READY + v5 RANDOM processing runs alongside the
                                # proven metadata detector, using the SAME received BGR frame.
                                if locked_side in ('1P', '2P') and pre_play_detected and not current_play_ended:
                                    gr_score, gx, gy, gw, gh, gs = find_best_get_ready_match(
                                        bgr, get_ready_templates, locked_side)
                                    if gr_state == GR_WAITING:
                                        gr_appear_count = (gr_appear_count + 1
                                                           if gr_score >= GET_READY_APPEAR_SCORE else 0)
                                        if gr_appear_count >= GET_READY_APPEAR_FRAMES:
                                            gr_state = GR_VISIBLE
                                            gr_disappear_count = 0
                                            print(f'[GET READY] score={gr_score:.3f} pos=({gx},{gy})')
                                    elif gr_state == GR_VISIBLE:
                                        gr_disappear_count = (gr_disappear_count + 1
                                                              if gr_score <= GET_READY_DISAPPEAR_SCORE else 0)
                                        if gr_disappear_count >= GET_READY_DISAPPEAR_FRAMES:
                                            gr_state = GR_PLAY_STARTED
                                            print('[GET READY DISAPPEARED] >>> PLAY START <<<')
                                            play_started_for_end = True
                                            current_play_ended = False
                                            end_frame_count = 0
                                            if play_end_detector is not None:
                                                play_end_detector.arm()
                                            publish(False, "", phase="PLAYING")
                                            # Select exactly one mode at PLAY START.
                                            # Existing RANDOM analysis remains unchanged.
                                            normal_recorder = None
                                            if (chart_ready and chart_events and
                                                    option_confirmed in ('RANDOM', 'R-RANDOM', 'MIRROR') and
                                                    song_id_confirmed is not None):
                                                random_analyzer = RandomMappingAnalyzer(chart_events)
                                                (note_detector if locked_side == '1P' else note_detector_2p).reset_detection_session()
                                                play_active = True
                                                print(f'[RANDOM ANALYSIS START] song_id={song_id_confirmed} '
                                                      f'difficulty={difficulty_confirmed} '
                                                      f'normal_events={len(chart_events)} candidates=5040')
                                            elif (option_confirmed == 'NORMAL' and
                                                  song_id_confirmed is not None and
                                                  difficulty_confirmed is not None and
                                                  level_confirmed is not None):
                                                # Check the DB directly: a level mismatch must
                                                # never be silently overwritten or re-recorded.
                                                existing = conn.execute(
                                                    'SELECT level FROM normal_charts WHERE song_id=? AND difficulty=?',
                                                    (song_id_confirmed, difficulty_confirmed)).fetchone()
                                                if existing is not None:
                                                    play_active = False
                                                    print(f'[NORMAL RECORD SKIPPED] chart already exists '
                                                          f'song_id={song_id_confirmed} difficulty={difficulty_confirmed} '
                                                          f'DB_level={existing[0]} screen_level={level_confirmed}')
                                                else:
                                                    normal_recorder = NormalChartRecorder(
                                                        song_id_confirmed, difficulty_confirmed, level_confirmed)
                                                    random_analyzer = None
                                                    (note_detector if locked_side == '1P' else note_detector_2p).reset_detection_session()
                                                    play_active = True
                                                    print(f'[NORMAL RECORD START] song_id={song_id_confirmed} '
                                                          f'difficulty={difficulty_confirmed} level={level_confirmed}')
                                            else:
                                                play_active = False
                                                print(f'[ANALYSIS SKIPPED] chart_ready={chart_ready} '
                                                      f'option={option_confirmed} song_id={song_id_confirmed} '
                                                      f'difficulty={difficulty_confirmed} level={level_confirmed}')
                                if play_active and (random_analyzer is not None or normal_recorder is not None):
                                    active_note_detector = note_detector if locked_side == '1P' else note_detector_2p
                                    lane_state = active_note_detector.detect_lanes(bgr)
                                    event_no, rising_lanes = active_note_detector.register_events(lane_state)
                                    if rising_lanes:
                                        print(f'[EVENT {event_no}] lanes={lanes_text(rising_lanes)}')
                                        if normal_recorder is not None:
                                            if normal_recorder.add_event(rising_lanes):
                                                play_active = False
                                                try:
                                                    count = normal_recorder.save(data_dir / DB_FILENAME)
                                                    print(f'[NORMAL CHART REGISTERED] song_id={normal_recorder.song_id} '
                                                          f'difficulty={normal_recorder.difficulty} '
                                                          f'level={normal_recorder.level} events={count}')
                                                except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                                                    print(f'[NORMAL CHART SAVE ERROR] {exc}; no overwrite')
                                                normal_recorder = None
                                                print('[NORMAL RECORD STOP]')
                                        elif random_analyzer is not None:
                                            random_analyzer.process(rising_lanes, event_no)
                                            if random_analyzer.confirmed:
                                                publish(True, random_analyzer.layout, phase="CONFIRMED")
                                                if direct_overlay is not None:
                                                    direct_overlay.show_layout(random_analyzer.layout)
                                                print(f"[DISPLAY UPDATED] layout={random_analyzer.layout}")
                                            if random_analyzer.confirmed or random_analyzer.failed:
                                                play_active = False
                                                print('[RANDOM ANALYSIS STOP] '
                                                      + ('CONFIRMED' if random_analyzer.confirmed else 'FAILED'))
                                # After PLAY END, independently watch for the SIDE banner.
                                # Keep locked_side intact: quick retry can reach PRE_PLAY
                                # without ever showing a selection screen.
                                if current_play_ended and current_display_phase == "ENDED":
                                    lx1, ly1, lx2, ly2 = scaled_roi(LEFT_SIDE_ROI_1920, width, height)
                                    rx1, ry1, rx2, ry2 = scaled_roi(RIGHT_SIDE_ROI_1920, width, height)
                                    left_gray = cv2.cvtColor(bgr[ly1:ly2, lx1:lx2], cv2.COLOR_BGR2GRAY)
                                    right_gray = cv2.cvtColor(bgr[ry1:ry2, rx1:rx2], cv2.COLOR_BGR2GRAY)
                                    slp, _ = match_one(left_gray, t01, m01)
                                    srp, _ = match_one(right_gray, t02, m02)
                                    sln, _ = match_one(left_gray, tno, mno)
                                    srn, _ = match_one(right_gray, tno, mno)
                                    lp, rp = slp >= SIDE_MIN_SCORE, srp >= SIDE_MIN_SCORE
                                    ln, rn = sln >= SIDE_MIN_SCORE, srn >= SIDE_MIN_SCORE
                                    return_hit = None
                                    if lp and rn and not ln and not rp:
                                        return_hit = "1P"
                                    elif ln and rp and not lp and not rn:
                                        return_hit = "2P"
                                    if return_hit is None:
                                        return_side_candidate = None
                                        return_side_count = 0
                                    else:
                                        if return_hit == return_side_candidate:
                                            return_side_count += 1
                                        else:
                                            return_side_candidate = return_hit
                                            return_side_count = 1
                                        if return_side_count >= SIDE_CONFIRM_FRAMES:
                                            locked_side = return_hit
                                            apply_direct_side(locked_side)
                                            if history_overlay is not None:
                                                history_overlay.set_side(locked_side)
                                            side_candidate = None
                                            side_candidate_count = 0
                                            return_side_candidate = None
                                            return_side_count = 0
                                            pre_candidate_count = 0
                                            pre_play_detected = False
                                            print(f"[SIDE RELOCK] {locked_side} x{SIDE_CONFIRM_FRAMES} -> SELECTING", flush=True)
                                            publish(False, "", phase="SELECTING")

                                if locked_side is None:
                                    lx1, ly1, lx2, ly2 = scaled_roi(LEFT_SIDE_ROI_1920, width, height)
                                    rx1, ry1, rx2, ry2 = scaled_roi(RIGHT_SIDE_ROI_1920, width, height)

                                    left_gray = cv2.cvtColor(bgr[ly1:ly2, lx1:lx2], cv2.COLOR_BGR2GRAY)
                                    right_gray = cv2.cvtColor(bgr[ry1:ry2, rx1:rx2], cv2.COLOR_BGR2GRAY)

                                    slp, _ = match_one(left_gray, t01, m01)
                                    srp, _ = match_one(right_gray, t02, m02)
                                    sln, _ = match_one(left_gray, tno, mno)
                                    srn, _ = match_one(right_gray, tno, mno)

                                    lp = slp >= SIDE_MIN_SCORE
                                    rp = srp >= SIDE_MIN_SCORE
                                    ln = sln >= SIDE_MIN_SCORE
                                    rn = srn >= SIDE_MIN_SCORE

                                    hit_side = None
                                    if lp and rn and not ln and not rp:
                                        hit_side = "1P"
                                    elif ln and rp and not lp and not rn:
                                        hit_side = "2P"

                                    if hit_side:
                                        if hit_side == side_candidate:
                                            side_candidate_count += 1
                                        else:
                                            side_candidate = hit_side
                                            side_candidate_count = 1

                                        if side_candidate_count >= SIDE_CONFIRM_FRAMES:
                                            locked_side = hit_side
                                            apply_direct_side(locked_side)
                                            if history_overlay is not None:
                                                history_overlay.set_side(locked_side)
                                            pre_candidate_count = 0
                                            pre_play_detected = False
                                            print(
                                                f"[SIDE LOCK] {locked_side} "
                                                f"L_PLAYER={slp:.3f} L_NO={sln:.3f}  "
                                                f"R_PLAYER={srp:.3f} R_NO={srn:.3f} "
                                                f"x{side_candidate_count}"
                                            )
                                            roi_ref = scratch_roi_1920(locked_side, sw, sh)
                                            print(
                                                f"[PRE_PLAY SEARCH START] side={locked_side} "
                                                f"ROI={roi_ref} threshold={SCRATCH_THRESHOLD[locked_side]:.2f}"
                                            )
                                    else:
                                        side_candidate = None
                                        side_candidate_count = 0

                                elif not play_active:
                                    roi_ref = scratch_roi_1920(locked_side, sw, sh)
                                    x1, y1, x2, y2 = scaled_roi(roi_ref, width, height)
                                    search_gray = cv2.cvtColor(
                                        bgr[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY
                                    )
                                    score, local_pos = match_one(search_gray, ts, ms)
                                    latest_scratch_score = score

                                    if local_pos is not None:
                                        latest_scratch_pos = (x1 + local_pos[0], y1 + local_pos[1])
                                    else:
                                        latest_scratch_pos = None


                                    threshold = SCRATCH_THRESHOLD[locked_side]
                                    if score >= threshold:
                                        pre_candidate_count += 1
                                    else:
                                        pre_candidate_count = 0
                                        pre_play_detected = False

                                    if (
                                        not pre_play_detected
                                        and pre_candidate_count >= SCRATCH_CONFIRM_FRAMES
                                    ):
                                        pre_play_detected = True
                                        print(
                                            f"[PRE_PLAY DETECTED] side={locked_side} "
                                            f"score={score:.3f} pos={latest_scratch_pos} "
                                            f"x{SCRATCH_CONFIRM_FRAMES}"
                                        )

                                        play_started_for_end = False
                                        current_play_ended = False
                                        return_side_candidate = None
                                        return_side_count = 0
                                        if play_end_detector is not None:
                                            play_end_detector.armed = False
                                        publish(False, "", phase="PRE_PLAY")
                                        if direct_overlay is not None:
                                            direct_overlay.clear()
                                        pre_play_t0 = time.perf_counter()
                                        option_anchor_first_t = None
                                        option_first_class_t = None
                                        option_candidate = None
                                        option_candidate_count = 0
                                        option_confirmed = None
                                        option_last_result = "UNKNOWN"
                                        detected_layout = None
                                        layout_candidate = None
                                        layout_candidate_count = 0
                                        difficulty_candidate = None
                                        difficulty_candidate_count = 0
                                        difficulty_confirmed = None
                                        difficulty_last = "UNKNOWN"
                                        difficulty_last_hue = None
                                        level_candidate = None
                                        level_candidate_count = 0
                                        level_confirmed = None
                                        level_last = None
                                        level_last_score = 0.0
                                        level_last_margin = 0.0
                                        title_roi_saved = False
                                        title_roi_last_coords = None
                                        song_ocr_attempted = False
                                        song_id_confirmed = None
                                        chart_ready = False
                                        chart_events = None
                                        random_analyzer = None
                                        if normal_recorder is not None:
                                            normal_recorder.cancel()
                                            normal_recorder = None
                                            play_active = False
                                            print('[NORMAL RECORD CANCELLED] new PRE_PLAY before completion')
                                        gr_state = GR_WAITING
                                        gr_appear_count = 0
                                        gr_disappear_count = 0
                                        ocr_generation += 1
                                        try:
                                            cv2.destroyWindow(TITLE_WINDOW_NAME)
                                        except cv2.error:
                                            pass
                                        print("[OPTION TIMING START] t=0.000s at PRE_PLAY DETECTED")


                                # --------------------------------------------------
                                # OPTION timing measurement.
                                # No fixed wait is used here: measure actual delay
                                # from PRE_PLAY detection to usable OPTION signals.
                                # --------------------------------------------------
                                if pre_play_t0 is not None:
                                    (
                                        anchor_score,
                                        layout_now,
                                        anchor_abs_pos,
                                        layout_scores,
                                    ) = detect_layout_anchor(
                                        bgr, locked_side, option_templates["ANCHOR"]
                                    )
                                    option_last_anchor = anchor_score

                                    if anchor_score >= ANCHOR_THRESHOLD:
                                        now_opt = time.perf_counter()

                                        # Layout itself also gets x3 confirmation.
                                        if layout_now == layout_candidate:
                                            layout_candidate_count += 1
                                        else:
                                            layout_candidate = layout_now
                                            layout_candidate_count = 1

                                        if (
                                            detected_layout is None
                                            and layout_candidate_count >= 3
                                        ):
                                            detected_layout = layout_now
                                            score_text = " ".join(
                                                f"{name}:{score:.3f}"
                                                for score, name, _ in layout_scores
                                            )
                                            print(
                                                f"[LAYOUT DETECTED] {detected_layout} "
                                                f"anchor={anchor_score:.3f} "
                                                f"pos={anchor_abs_pos} x3 "
                                                f"[{score_text}]"
                                            )

                                            title_crop, title_coords = get_title_roi(
                                                bgr, detected_layout
                                            )
                                            title_roi_last_coords = title_coords
                                            if SHOW_TITLE_PREVIEW and title_crop is not None and title_crop.size:
                                                preview = cv2.resize(
                                                    title_crop,
                                                    None,
                                                    fx=TITLE_PREVIEW_SCALE,
                                                    fy=TITLE_PREVIEW_SCALE,
                                                    interpolation=cv2.INTER_NEAREST,
                                                )
                                                cv2.imshow(TITLE_WINDOW_NAME, preview)

                                                if SAVE_TITLE_ROI and not title_roi_saved:
                                                    stamp = time.strftime("%Y%m%d_%H%M%S")
                                                    out_path = (
                                                        app_root()
                                                        / f"title_roi_{detected_layout}_{stamp}.png"
                                                    )
                                                    if save_png_unicode(out_path, title_crop):
                                                        print(
                                                            f"[TITLE ROI SAVED] "
                                                            f"layout={detected_layout} "
                                                            f"ROI={title_coords} "
                                                            f"size={title_crop.shape[1]}x{title_crop.shape[0]} "
                                                            f"path={out_path}"
                                                        )
                                                    else:
                                                        print(
                                                            f"[TITLE ROI SAVE FAILED] "
                                                            f"layout={detected_layout} ROI={title_coords}"
                                                        )
                                                    title_roi_saved = True

                                        # Metadata is read only after layout x3 is confirmed.
                                        if detected_layout is not None:
                                            title_crop, title_coords = get_title_roi(
                                                bgr, detected_layout
                                            )
                                            title_roi_last_coords = title_coords
                                            if SHOW_TITLE_PREVIEW and title_crop is not None and title_crop.size:
                                                preview = cv2.resize(
                                                    title_crop,
                                                    None,
                                                    fx=TITLE_PREVIEW_SCALE,
                                                    fy=TITLE_PREVIEW_SCALE,
                                                    interpolation=cv2.INTER_NEAREST,
                                                )
                                                cv2.imshow(TITLE_WINDOW_NAME, preview)
                                            if (not song_ocr_attempted
                                                    and difficulty_confirmed is not None
                                                    and level_confirmed is not None
                                                    and title_crop is not None
                                                    and title_crop.size):
                                                song_ocr_attempted = True
                                                print(f"[TITLE OCR START] layout={detected_layout} "
                                                      f"difficulty={difficulty_confirmed} level={level_confirmed}")
                                                threading.Thread(
                                                    target=title_ocr_worker,
                                                    args=(title_crop.copy(), data_dir / DB_FILENAME,
                                                          ocr_generation, ocr_results),
                                                    daemon=True,
                                                    name="iidx-title-ocr",
                                                ).start()
                                                print("[TITLE OCR ASYNC] worker started; capture loop continues", flush=True)
                                            diff, hue, pix_n, diff_dist = detect_difficulty_layout(
                                                bgr, detected_layout
                                            )
                                            difficulty_last = diff
                                            difficulty_last_hue = hue
                                            if diff != "UNKNOWN":
                                                if diff == difficulty_candidate:
                                                    difficulty_candidate_count += 1
                                                else:
                                                    difficulty_candidate = diff
                                                    difficulty_candidate_count = 1
                                                if (
                                                    difficulty_confirmed is None
                                                    and difficulty_candidate_count >= METADATA_CONFIRM_FRAMES
                                                ):
                                                    difficulty_confirmed = diff
                                                    print(
                                                        f"[DIFFICULTY DETECTED] "
                                                        f"layout={detected_layout} "
                                                        f"{diff} hue={hue} pixels={pix_n} "
                                                        f"x{METADATA_CONFIRM_FRAMES}"
                                                    )
                                            else:
                                                difficulty_candidate = None
                                                difficulty_candidate_count = 0

                                            (
                                                lv,
                                                lv_score,
                                                lv_margin,
                                                lv_scores,
                                            ) = detect_level_layout(
                                                bgr, detected_layout
                                            )
                                            level_last = lv
                                            level_last_score = lv_score
                                            level_last_margin = lv_margin
                                            if lv is not None:
                                                if lv == level_candidate:
                                                    level_candidate_count += 1
                                                else:
                                                    level_candidate = lv
                                                    level_candidate_count = 1
                                                if (
                                                    level_confirmed is None
                                                    and level_candidate_count >= METADATA_CONFIRM_FRAMES
                                                ):
                                                    level_confirmed = lv
                                                    print(
                                                        f"[LEVEL DETECTED] "
                                                        f"layout={detected_layout} "
                                                        f"LV.{lv} score={lv_score:.3f} "
                                                        f"margin={lv_margin:.3f} "
                                                        f"x{METADATA_CONFIRM_FRAMES}"
                                                    )
                                            else:
                                                level_candidate = None
                                                level_candidate_count = 0

                                        if option_anchor_first_t is None:
                                            option_anchor_first_t = now_opt
                                            print(
                                                f"[OPTION ANCHOR FIRST] "
                                                f"+{now_opt - pre_play_t0:.3f}s "
                                                f"layout={layout_now} "
                                                f"score={anchor_score:.3f} "
                                                f"pos={anchor_abs_pos}"
                                            )

                                        abs_ax, abs_ay = anchor_abs_pos
                                        sx = width / REF_W
                                        sy = height / REF_H
                                        rx1, ry1, rx2, ry2 = OPTION_REL
                                        ox1 = max(0, int(round(abs_ax + rx1 * sx)))
                                        oy1 = max(0, int(round(abs_ay + ry1 * sy)))
                                        ox2 = min(width, int(round(abs_ax + rx2 * sx)))
                                        oy2 = min(height, int(round(abs_ay + ry2 * sy)))

                                        option_gray = cv2.cvtColor(
                                            bgr[oy1:oy2, ox1:ox2],
                                            cv2.COLOR_BGR2GRAY,
                                        )
                                        (
                                            result,
                                            random_score,
                                            mirror_score,
                                            dbg,
                                        ) = classify_option(
                                            option_gray, option_templates
                                        )
                                        # OFF/LEGACY: the validated anchor locates the
                                        # option region; no RANDOM/MIRROR match means NORMAL.
                                        # Keep the original classify_option() unchanged.
                                        # This branch is reached only after anchor threshold
                                        # and a nonempty option ROI have been established.
                                        if result == "UNKNOWN":
                                            result = "NORMAL"
                                        option_last_result = result
                                        option_last_random = random_score
                                        option_last_mirror = mirror_score
                                        option_last_r = dbg["r_score"]
                                        option_last_s = dbg["s_score"]

                                        if (
                                            result != "UNKNOWN"
                                            and option_first_class_t is None
                                        ):
                                            option_first_class_t = now_opt
                                            print(
                                                f"[OPTION FIRST CLASS] "
                                                f"+{now_opt - pre_play_t0:.3f}s "
                                                f"layout={layout_now} "
                                                f"result={result} "
                                                f"RANDOM={random_score:.3f} "
                                                f"MIRROR={mirror_score:.3f} "
                                                f"R-={dbg['r_score']:.3f} "
                                                f"S-={dbg['s_score']:.3f}"
                                            )

                                        if result == option_candidate:
                                            option_candidate_count += 1
                                        else:
                                            option_candidate = result
                                            option_candidate_count = 1

                                        if (
                                            result != "UNKNOWN"
                                            and option_confirmed is None
                                            and option_candidate_count >= OPTION_CONFIRM_FRAMES
                                        ):
                                            option_confirmed = result
                                            print(
                                                f"[OPTION STABLE x{OPTION_CONFIRM_FRAMES}] "
                                                f"+{now_opt - pre_play_t0:.3f}s "
                                                f"layout={layout_now} "
                                                f"result={result} "
                                                f"anchor={anchor_score:.3f} "
                                                f"RANDOM={random_score:.3f} "
                                                f"MIRROR={mirror_score:.3f} "
                                                f"R-={dbg['r_score']:.3f} "
                                                f"S-={dbg['s_score']:.3f}"
                                            )
                                    else:
                                        layout_candidate = None
                                        layout_candidate_count = 0
                                        option_candidate = None
                                        option_candidate_count = 0

                                interval_frames += 1

                key = cv2.waitKey(1) & 0xFF
                if not args.launcher_mode and key in (27, ord("q"), ord("Q")):
                    return 0
                if not args.launcher_mode and windows_quit_pressed():
                    print("[QUIT] Windows key fallback.")
                    return 0

                if launcher_stop.is_set():
                    print("[QUIT] Launcher requested graceful stop.", flush=True)
                    return 0

                now = time.perf_counter()
                dt = now - report_t0
                if dt >= 1.0:
                    fps = interval_frames / dt
                    if connected and last_size:
                        if locked_side is None:
                            state_signature = ('WAIT_SIDE', side_candidate or 'SEARCHING')
                            state_line = f"[STATE] SIDE={side_candidate or 'SEARCHING'} PRE_PLAY=WAIT_SIDE"
                        else:
                            pre_state = 'DETECTED' if pre_play_detected else 'SEARCHING'
                            state_signature = ('LOCKED', locked_side, pre_state, current_display_phase)
                            state_line = (f"[STATE] SIDE=LOCKED {locked_side} PRE_PLAY={pre_state} "
                                          f"PHASE={current_display_phase} HISTORY_VISIBLE={history_visible(display_settings, current_display_phase, locked_side or '1P')} "
                                          f"HISTORY_COUNT={len(session_history)}")
                    else:
                        state_signature = ('WAIT_SENDER',)
                        state_line = f"[STATE] waiting for {SENDER_NAME} ..."
                    if state_signature != last_state_signature:
                        print(state_line, flush=True)
                        last_state_signature = state_signature
                    interval_frames = 0
                    report_t0 = now

                if not connected:
                    time.sleep(0.01)
                if direct_overlay is not None:
                    direct_overlay.pump()
                pace_60(loop_start)

    finally:
        publish(False, "", phase="CLOSED")
        if history_overlay is not None:
            history_overlay.close()
        if direct_overlay is not None:
            direct_overlay.close()
        cv2.destroyAllWindows()
        conn.close()
        pygame.quit()
        print("[CLOSED]")


if __name__ == "__main__":
    sys.exit(main())
