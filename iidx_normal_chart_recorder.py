"""IIDX normal-chart event recorder and SQLite persistence (no capture/UI).

Integration contract:
- Caller MUST verify the game is in true NORMAL lane arrangement (no MIRROR,
  RANDOM, R-RANDOM, S-RANDOM, etc.). UNKNOWN is NOT confirmation.
- Call start only after song_id/difficulty/level and normal option are verified,
  at GET READY disappearance; feed rising-edge lanes from the existing v5 detector.
- A prefix is complete only if it uniquely distinguishes the identity permutation
  among all 5040 mappings (not merely because seven lanes have appeared).
- No overwrite and no partial-chart writes.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from pathlib import Path

LANES = frozenset(range(1, 8))
DIFFICULTIES = frozenset(("BEGINNER", "NORMAL", "HYPER", "ANOTHER", "LEGGENDARIA"))


class NormalChartRecorder:
    def __init__(self, song_id: int, difficulty: str, level: int):
        self.song_id = int(song_id)
        self.difficulty = str(difficulty).upper()
        self.level = int(level)
        if self.song_id <= 0 or self.difficulty not in DIFFICULTIES or not 1 <= self.level <= 12:
            raise ValueError("Invalid song_id/difficulty/level")
        self.events = []
        self.seen_lanes = set()
        self.finished = False
        self.cancelled = False

    def add_event(self, rising_lanes):
        """Feed a single logical v5 rising-edge event; return True if complete."""
        if self.finished or self.cancelled:
            return self.finished
        lanes = tuple(sorted(set(int(x) for x in rising_lanes)))
        if not lanes:
            return False
        if not set(lanes).issubset(LANES):
            raise ValueError(f"Invalid lane event: {lanes}")
        self.events.append({"event_no": len(self.events) + 1, "event_lanes": list(lanes)})
        self.seen_lanes.update(lanes)
        if self.seen_lanes == LANES:
            self.finished = self._identity_unique()
        return self.finished

    def _identity_unique(self):
        """Require that only the identity mapping fits all recorded lane sets."""
        for p in itertools.permutations(range(1, 8)):
            if p == (1, 2, 3, 4, 5, 6, 7):
                continue
            if all({p[lane - 1] for lane in e["event_lanes"]} == set(e["event_lanes"])
                   for e in self.events):
                return False
        return True

    def cancel(self):
        self.cancelled = True
        self.events.clear()

    def save(self, db_path: str | Path):
        """Insert complete chart atomically into existing schema; never overwrite."""
        if self.cancelled or not self.finished or not self.events:
            raise RuntimeError("Incomplete/cancelled chart: database unchanged")
        with sqlite3.connect(str(db_path), timeout=5.0) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            if conn.execute("SELECT 1 FROM songs WHERE song_id=?", (self.song_id,)).fetchone() is None:
                raise ValueError(f"Unknown song_id={self.song_id}: database unchanged")
            try:
                conn.execute(
                    "INSERT INTO normal_charts(song_id,difficulty,level,note_events) VALUES(?,?,?,?)",
                    (self.song_id, self.difficulty, self.level,
                     json.dumps(self.events, ensure_ascii=False)),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError("Chart already registered or invalid: database unchanged") from exc
        return len(self.events)
