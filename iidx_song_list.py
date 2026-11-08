"""Official INFINITAS song-list downloader and local candidate database.

The official list is deliberately stored separately from iidx_songs.sqlite3.
Only the selected categories are imported; songs missing from a later page are
never deleted from the local database.
"""
from __future__ import annotations

import hashlib
import html
from html.parser import HTMLParser
import json
import re
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from iidx_paths import app_root

SOURCE_URL = "https://p.eagate.573.jp/game/infinitas/2/music/index.html"
DB_PATH = app_root() / "data" / "iidx_official_song_list.sqlite3"
INCLUDED_CATEGORIES = ("新規追加曲", "初期収録曲", "DJP解禁曲", "BIT解禁曲", "楽曲パック")

# OCR title matching: exact matches are unconditional; fuzzy matches require
# both a minimum similarity and a sufficient lead over the runner-up.
OFFICIAL_TITLE_MIN_SCORE = 0.50
OFFICIAL_TITLE_MIN_MARGIN = 0.20


class _MusicPageParser(HTMLParser):
    """Extract two-cell title/artist rows while following visible section labels."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.category = None
        self.pack_name = None
        self.added_date = None
        self.rows = []
        self.in_tr = False
        self.in_cell = False
        self.cell = []
        self.cells = []
        self.outside_buffer = ""
        self.in_strong = False
        self.strong_buffer = []

    def _outside(self, value):
        value = " ".join(html.unescape(value).split())
        if not value:
            return
        self.outside_buffer = (self.outside_buffer + " " + value).strip()[-500:]
        value = self.outside_buffer
        date_match = re.search(r"(20\d{2})\s*[年/.\-]\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})\s*日?\s*追加", value)
        if date_match:
            y, m, d = map(int, date_match.groups())
            self.added_date = f"{y:04d}-{m:02d}-{d:02d}"
            self.category = "新規追加曲"
            self.outside_buffer = ""
        elif "新規追加曲" in value:
            self.category = "新規追加曲"
            self.outside_buffer = ""
        elif "トライアルモード" in value:
            self.category = "除外"
            self.outside_buffer = ""
        elif "初期収録曲" in value:
            self.category = "初期収録曲"
            self.outside_buffer = ""
        elif "DJP解禁曲" in value:
            self.category = "DJP解禁曲"
            self.outside_buffer = ""
        elif "BIT解禁曲" in value:
            self.category = "BIT解禁曲"
            self.outside_buffer = ""
        elif "LEGGENDARIA" in value:
            self.category = "除外"
            self.outside_buffer = ""
        # Pack names are captured separately from <strong> elements.
        # Do not infer them from the surrounding text buffer, which may also
        # contain neighboring <span> notes such as "※30曲収録".

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
            self.in_tr = True
            self.cells = []
        elif self.in_tr and tag in ("td", "th"):
            self.in_cell = True
            self.cell = []
        elif tag == "strong" and not self.in_tr:
            self.in_strong = True
            self.strong_buffer = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "strong" and self.in_strong:
            strong_text = " ".join("".join(self.strong_buffer).split())
            if "楽曲パック" in strong_text:
                self.category = "楽曲パック"
                self.pack_name = strong_text
                self.outside_buffer = ""
            self.in_strong = False
            self.strong_buffer = []
        elif self.in_tr and tag in ("td", "th") and self.in_cell:
            self.cells.append(" ".join("".join(self.cell).split()))
            self.cell = []
            self.in_cell = False
        elif tag == "tr" and self.in_tr:
            cells = [x for x in self.cells]
            if len(cells) >= 2:
                title, artist = cells[0].strip(), cells[1].strip()
                if title and artist and title not in ("タイトル", "曲名") and artist not in ("アーティスト名", "カテゴリー"):
                    self.rows.append({
                        "title": title, "artist": artist, "category": self.category,
                        "added_date": self.added_date if self.category == "新規追加曲" else None,
                        "pack_name": self.pack_name if self.category == "楽曲パック" else None,
                    })
            self.in_tr = False
            self.in_cell = False
            self.cell = []
            self.cells = []

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)
        elif not self.in_tr:
            if self.in_strong:
                self.strong_buffer.append(data)
            self._outside(data)


def parse_music_page(document: str):
    parser = _MusicPageParser()
    parser.feed(document)
    parser.close()
    rows = [r for r in parser.rows if r["category"] in INCLUDED_CATEGORIES]
    if not rows:
        raise ValueError("公式ページから対象カテゴリの楽曲行を取得できませんでした。ページ構造が変わった可能性があります。")
    categories = {r["category"] for r in rows}
    if "楽曲パック" not in categories or not ({"新規追加曲", "初期収録曲", "DJP解禁曲", "BIT解禁曲"} & categories):
        raise ValueError("公式ページのカテゴリ構造を十分に確認できませんでした。既存データは更新しません。")
    return rows


def _connect(path=DB_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS official_songs (
            title TEXT NOT NULL,
            artist TEXT NOT NULL,
            categories_json TEXT NOT NULL DEFAULT '[]',
            added_date TEXT,
            pack_names_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY(title, artist)
        );
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    return conn


def _signature(rows):
    additions = sorted((r["added_date"] or "", r["title"], r["artist"])
                       for r in rows if r["category"] == "新規追加曲")
    packs = sorted((r["pack_name"] or "", r["title"], r["artist"])
                   for r in rows if r["category"] == "楽曲パック")
    newest_pack = next((r["pack_name"] for r in rows if r["category"] == "楽曲パック"), "")
    payload = json.dumps({"additions": additions, "packs": packs,
                          "newest_pack": newest_pack}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), newest_pack


def update_official_song_list(timeout=25, db_path=DB_PATH, replace_all=False):
    """Fetch and validate, then upsert or atomically replace all official song rows."""
    request = urllib.request.Request(SOURCE_URL, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    document = raw.decode(charset, errors="replace")
    rows = parse_music_page(document)
    signature, newest_pack = _signature(rows)

    conn = _connect(db_path)
    try:
        old = conn.execute("SELECT value FROM metadata WHERE key='content_signature'").fetchone()
        changed = old is None or old[0] != signature
        # Group duplicate title/artist pairs across categories before writing.
        grouped = {}
        for row in rows:
            key = (row["title"], row["artist"])
            item = grouped.setdefault(key, {"categories": set(), "dates": set(), "packs": set()})
            item["categories"].add(row["category"])
            if row["added_date"]: item["dates"].add(row["added_date"])
            if row["pack_name"]: item["packs"].add(row["pack_name"])
        conn.execute("BEGIN IMMEDIATE")
        if replace_all:
            # Delete only after fetching and validating the complete source page.
            # The transaction protects the previous DB if any subsequent write fails.
            conn.execute("DELETE FROM official_songs")
        for (title, artist), item in grouped.items():
            existing = conn.execute("SELECT categories_json, added_date, pack_names_json FROM official_songs WHERE title=? AND artist=?", (title, artist)).fetchone()
            categories = set(json.loads(existing[0])) if existing else set()
            packs = set(json.loads(existing[2])) if existing else set()
            categories.update(item["categories"])
            packs.update(item["packs"])
            date_values = item["dates"]
            added_date = max(date_values) if date_values else (existing[1] if existing else None)
            conn.execute("""INSERT INTO official_songs(title,artist,categories_json,added_date,pack_names_json)
                VALUES(?,?,?,?,?) ON CONFLICT(title,artist) DO UPDATE SET
                categories_json=excluded.categories_json,
                added_date=COALESCE(excluded.added_date, official_songs.added_date),
                pack_names_json=excluded.pack_names_json""",
                (title, artist, json.dumps(sorted(categories), ensure_ascii=False), added_date,
                 json.dumps(sorted(packs), ensure_ascii=False)))
        conn.execute("INSERT INTO metadata(key,value) VALUES('content_signature',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (signature,))
        conn.execute("INSERT INTO metadata(key,value) VALUES('last_checked_utc',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (datetime.now(timezone.utc).isoformat(),))
        conn.execute("INSERT INTO metadata(key,value) VALUES('newest_pack_name',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (newest_pack,))
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM official_songs").fetchone()[0]
        return {"changed": changed, "fetched": len(rows), "unique": len(grouped),
                "total": total, "newest_pack": newest_pack, "db_path": str(db_path),
                "replaced_all": bool(replace_all)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()



def diagnose_official_title_match(ocr_text, db_path=DB_PATH, limit=3):
    """Return DB availability and nearest title scores for diagnostic logging."""
    import difflib
    import unicodedata
    path = Path(db_path)
    result = {
        "db_path": str(path),
        "db_exists": path.is_file(),
        "song_count": 0,
        "status": "NO_LIST",
        "best_title": None,
        "best_score": 0.0,
        "second_title": None,
        "second_score": 0.0,
        "threshold": OFFICIAL_TITLE_MIN_SCORE,
        "margin_threshold": OFFICIAL_TITLE_MIN_MARGIN,
    }
    needle = " ".join(unicodedata.normalize("NFKC", str(ocr_text)).casefold().split())
    if not needle or not path.is_file():
        return result
    try:
        conn = sqlite3.connect(str(path), timeout=3)
        try:
            result["song_count"] = conn.execute(
                "SELECT COUNT(*) FROM official_songs"
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT title, COUNT(DISTINCT artist) FROM official_songs GROUP BY title"
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        result["status"] = "DB_ERROR"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    if not rows:
        result["status"] = "EMPTY_LIST"
        return result
    candidates = [title for title, artist_count in rows if artist_count == 1]
    if not candidates:
        result["status"] = "AMBIGUOUS_TITLE"
        return result
    scores = sorted(
        ((difflib.SequenceMatcher(
            None, needle,
            " ".join(unicodedata.normalize("NFKC", title).casefold().split())
        ).ratio(), title) for title in candidates),
        reverse=True,
    )
    if not scores:
        result["status"] = "EMPTY_LIST"
        return result
    best_score, best_title = scores[0]
    second_score, second_title = scores[1] if len(scores) > 1 else (0.0, None)
    result.update({
        "best_title": best_title,
        "best_score": best_score,
        "second_title": second_title,
        "second_score": second_score,
    })
    if best_score == 1.0 and second_score < 1.0:
        result["status"] = "EXACT"
    elif (best_score >= OFFICIAL_TITLE_MIN_SCORE and
          best_score - second_score >= OFFICIAL_TITLE_MIN_MARGIN):
        result["status"] = "FUZZY"
    else:
        result["status"] = "AMBIGUOUS_OR_UNKNOWN"
    return result

def match_official_title(ocr_text, db_path=DB_PATH):
    """Return a unique official title for a sufficiently close OCR string.

    Matching is title-only by design. Duplicate title entries (different artists)
    are treated as ambiguous instead of selecting an arbitrary artist.
    """
    import difflib
    import unicodedata
    needle = " ".join(unicodedata.normalize("NFKC", str(ocr_text)).casefold().split())
    if not needle or not Path(db_path).is_file():
        return None, "NO_LIST"
    conn = sqlite3.connect(str(db_path), timeout=3)
    try:
        rows = conn.execute("SELECT title, COUNT(DISTINCT artist) FROM official_songs GROUP BY title").fetchall()
    finally:
        conn.close()
    if not rows:
        return None, "EMPTY_LIST"
    candidates = [title for title, artist_count in rows if artist_count == 1]
    if not candidates:
        return None, "AMBIGUOUS_TITLE"
    scores = sorted(((difflib.SequenceMatcher(None, needle,
                     " ".join(unicodedata.normalize("NFKC", title).casefold().split())).ratio(), title)
                     for title in candidates), reverse=True)
    if not scores:
        return None, "EMPTY_LIST"
    best_score, best_title = scores[0]
    second = scores[1][0] if len(scores) > 1 else 0.0
    if best_score == 1.0 and second < 1.0:
        return best_title, "EXACT"
    if (best_score >= OFFICIAL_TITLE_MIN_SCORE and
            best_score - second >= OFFICIAL_TITLE_MIN_MARGIN):
        return best_title, "FUZZY"
    return None, "AMBIGUOUS_OR_UNKNOWN"
