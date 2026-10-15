"""Validated play-end template matcher (no Spout receiver)."""
from pathlib import Path
import cv2
import numpy as np

SENDER = 'Spout_OBS_Filter'
# 1920x1080 reference coordinates: stable distinctive central regions.
TEMPLATE_RECTS = {
    'BRIDGE': (710, 350, 940, 575),  # central blue circular INFINITAS emblem
    'FAILED': (530, 395, 1120, 555),  # STAGE FAILED lettering
}
SEARCH_MARGIN = {'BRIDGE': (100, 90), 'FAILED': (90, 75)}
DEFAULT_THRESHOLDS = {'BRIDGE': 0.72, 'FAILED': 0.72}
CONFIRM_FRAMES = 3


def imread_unicode(path):
    data = np.fromfile(str(path), dtype=np.uint8)
    if not data.size:
        raise FileNotFoundError(path)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f'Unable to decode {path}')
    return image


def gray_small(image, factor=0.5):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)


class EndDetector:
    def __init__(self, folder, threshold=0.72):
        self.templates = {}
        for name, filename in [('BRIDGE', 'Bridge_to_Result.png'), ('FAILED', 'Failed_to_Bridge.png')]:
            img = imread_unicode(folder / filename)
            if img.shape[:2] != (1080, 1920):
                raise ValueError(f'{filename} must be 1920x1080, got {img.shape[1]}x{img.shape[0]}')
            x1, y1, x2, y2 = TEMPLATE_RECTS[name]
            self.templates[name] = gray_small(img[y1:y2, x1:x2])
        self.threshold = threshold
        self.armed = False
        self.end_processed = False
        self.play_id = 0
        self.counts = {'BRIDGE': 0, 'FAILED': 0}

    def arm(self):
        self.play_id += 1
        self.armed = True
        self.end_processed = False
        self.counts = {'BRIDGE': 0, 'FAILED': 0}
        print(f'[PLAY ARMED] play_id={self.play_id}', flush=True)

    def check(self, frame):
        h, w = frame.shape[:2]
        scores = {}
        for name, template in self.templates.items():
            x1, y1, x2, y2 = TEMPLATE_RECTS[name]
            mx, my = SEARCH_MARGIN[name]
            # Scale the search region to the actual sender size.
            ax1 = max(0, round((x1 - mx) * w / 1920))
            ay1 = max(0, round((y1 - my) * h / 1080))
            ax2 = min(w, round((x2 + mx) * w / 1920))
            ay2 = min(h, round((y2 + my) * h / 1080))
            search = gray_small(frame[ay1:ay2, ax1:ax2])
            # Template is resized only if the source resolution differs.
            tw = max(1, round(template.shape[1] * w / 1920))
            th = max(1, round(template.shape[0] * h / 1080))
            tmpl = cv2.resize(template, (tw, th)) if (tw, th) != (template.shape[1], template.shape[0]) else template
            if search.shape[0] < th or search.shape[1] < tw:
                scores[name] = -1.0
                continue
            result = cv2.matchTemplate(search, tmpl, cv2.TM_CCOEFF_NORMED)
            scores[name] = float(cv2.minMaxLoc(result)[1])

        if self.armed and not self.end_processed:
            for name in ('FAILED', 'BRIDGE'):
                self.counts[name] = self.counts[name] + 1 if scores[name] >= self.threshold else 0
                if self.counts[name] >= CONFIRM_FRAMES:
                    self.end_processed = True
                    self.armed = False
                    print(f'[PLAY END DETECTED] play_id={self.play_id} type={name} '
                          f'score={scores[name]:.3f} (once only)', flush=True)
                    break
        return scores

