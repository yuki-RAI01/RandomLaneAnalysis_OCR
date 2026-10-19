# -*- coding: utf-8 -*-
"""Shared, image-free numeric features for single-letter IIDX header titles.
Feature version 1: density profiles, NOT pixel templates.
"""
import csv
from pathlib import Path
import cv2
import numpy as np

VERSION = 1
FRAME_SIZE = (1920, 1080)
DEFAULT_ROI = (1350, 54, 1530, 89)  # x1,y1,x2,y2; 1920x1080
PROFILE_BINS = 8
GRID_BINS = 6
FEATURE_NAMES = (['aspect', 'density'] +
                 [f'row_{i}' for i in range(PROFILE_BINS)] +
                 [f'col_{i}' for i in range(PROFILE_BINS)] +
                 [f'cell_{r}_{c}' for r in range(GRID_BINS) for c in range(GRID_BINS)])
COLUMNS = ['letter', 'version', 'sample_id'] + FEATURE_NAMES


def extract(frame, roi=DEFAULT_ROI):
    """Extract aggregate numerical descriptors from a BGR screenshot and ROI.
    Returns (features, diagnostics). Raises ValueError if not a single glyph.
    """
    if frame is None or frame.ndim != 3:
        raise ValueError('カラー画像が必要です')
    x1, y1, x2, y2 = map(int, roi)
    if not (0 <= x1 < x2 <= frame.shape[1] and 0 <= y1 < y2 <= frame.shape[0]):
        raise ValueError('切り出し範囲が画像の外にあります')
    crop = frame[y1:y2, x1:x2]
    hi = crop.max(axis=2).astype(np.int16)
    lo = crop.min(axis=2).astype(np.int16)
    mask = (lo >= 125) & ((hi - lo) <= 48)
    ys, xs = np.nonzero(mask)
    if len(xs) < 12:
        raise ValueError('曲名の白い画素が見つかりません')
    l, t, r, b = int(xs.min()), int(ys.min()), int(xs.max()+1), int(ys.max()+1)
    w, h = r-l, b-t
    if not (12 <= h <= 34 and 0.55 <= w/h <= 1.5):
        raise ValueError(f'1文字のサイズではありません: {w}x{h}')
    glyph = (mask[t:b, l:r].astype('uint8') * 255)
    # Resize to fixed dimensions, then aggregate into coarse cells.
    norm = cv2.resize(glyph, (GRID_BINS*8, GRID_BINS*8), interpolation=cv2.INTER_AREA).astype('float32')/255
    # Separate horizontal and vertical profiles plus coarse occupancy grid.
    row = [float(x.mean()) for x in np.array_split(norm, PROFILE_BINS, axis=0)]
    col = [float(x.mean()) for x in np.array_split(norm, PROFILE_BINS, axis=1)]
    cells = [float(norm[r*8:(r+1)*8,c*8:(c+1)*8].mean()) for r in range(GRID_BINS) for c in range(GRID_BINS)]
    values = [w/h, float(glyph.mean()/255)] + row + col + cells
    return dict(zip(FEATURE_NAMES, values)), {'width':w,'height':h,'bbox':(x1+l,y1+t,x1+r,y1+b),'roi':(x1,y1,x2,y2)}


def read_csv(path):
    path = Path(path)
    if not path.is_file():
        return []
    with path.open('r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != COLUMNS:
            raise ValueError('CSVの列構成が異なります')
        rows = []
        for n, row in enumerate(reader, 2):
            try:
                if int(row['version']) != VERSION: raise ValueError('version不一致')
                if len(row['letter']) != 1: raise ValueError('文字数が不正')
                vals = {k: float(row[k]) for k in FEATURE_NAMES}
                if not all(np.isfinite(v) for v in vals.values()): raise ValueError('非有限数値')
                rows.append({'letter':row['letter'], 'version':VERSION, 'sample_id':row['sample_id'], **vals})
            except (ValueError, TypeError, KeyError) as e:
                raise ValueError(f'CSV {n}行目: {e}') from e
        return rows


def write_csv(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow({k: (f'{row[k]:.6f}' if k in FEATURE_NAMES else row[k]) for k in COLUMNS})


def compare(features, rows):
    """Nearest exemplar per letter. Abstain on weak/ambiguous matches."""
    if not rows: return None, {'reason':'empty CSV'}
    # Weights keep aspect ratio and global density relevant without dominating.
    weights = np.array([1.4, 0.8] + [1.0]*16 + [0.65]*36, dtype=float)
    target = np.array([features[k] for k in FEATURE_NAMES], dtype=float)
    by_letter = {}
    for row in rows:
        vec = np.array([row[k] for k in FEATURE_NAMES], dtype=float)
        distance = float(np.sqrt(np.average((target-vec)**2, weights=weights)))
        by_letter[row['letter']] = min(distance, by_letter.get(row['letter'], float('inf')))
    ordered = sorted(by_letter.items(), key=lambda p:p[1])
    best = ordered[0]
    second = ordered[1] if len(ordered)>1 else ('',float('inf'))
    # Conservative defaults; tune only after cross-sample tests.
    accepted = best[1] <= 0.075 and second[1]-best[1] >= 0.035
    return (best[0] if accepted else None), {'best':best,'second':second,'accepted':accepted}
