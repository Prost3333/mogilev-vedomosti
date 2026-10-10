"""SQLite: состояние каждой плитки, находки, кандидаты, пакеты. Повторный запуск продолжает с места остановки."""
import json
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs(
    id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, pages INTEGER, size INTEGER);
CREATE TABLE IF NOT EXISTS tiles(
    id INTEGER PRIMARY KEY, doc_id INTEGER, page INTEGER, idx INTEGER,
    x INTEGER, y INTEGER, w INTEGER, h INTEGER, model TEXT,
    status TEXT DEFAULT 'pending', batch_id TEXT, response TEXT, error TEXT,
    in_tok INTEGER DEFAULT 0, out_tok INTEGER DEFAULT 0, updated TEXT,
    UNIQUE(doc_id, page, x, y, w, h, model));
CREATE TABLE IF NOT EXISTS hits(
    id INTEGER PRIMARY KEY, tile_id INTEGER, target TEXT, word TEXT, quote TEXT, certainty TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS candidates(
    id INTEGER PRIMARY KEY, doc_id INTEGER, page INTEGER, model TEXT, target TEXT,
    x INTEGER, y INTEGER, w INTEGER, h INTEGER, word TEXT, quote TEXT, certainty TEXT, tiles TEXT,
    status TEXT DEFAULT 'new', verify_model TEXT, verdict TEXT, fragment TEXT,
    in_tok INTEGER DEFAULT 0, out_tok INTEGER DEFAULT 0, error TEXT,
    UNIQUE(doc_id, page, model, target, x, y, w, h));
CREATE TABLE IF NOT EXISTS batches(
    id TEXT PRIMARY KEY, model TEXT, created TEXT, status TEXT, n INTEGER);
CREATE TABLE IF NOT EXISTS transcripts(
    id INTEGER PRIMARY KEY, doc_id INTEGER, page INTEGER, band INTEGER, model TEXT,
    y INTEGER, h INTEGER, status TEXT DEFAULT 'pending', text TEXT, error TEXT,
    in_tok INTEGER DEFAULT 0, out_tok INTEGER DEFAULT 0,
    UNIQUE(doc_id, page, band, model));
CREATE INDEX IF NOT EXISTS tiles_status ON tiles(status, model);
CREATE INDEX IF NOT EXISTS hits_tile ON hits(tile_id);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def q(self, sql, *args):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def x(self, sql, *args):
        with self.lock:
            return self.db.execute(sql, args)

    def doc(self, path, name, pages, size):
        self.x("INSERT OR IGNORE INTO docs(path, name, pages, size) VALUES(?,?,?,?)", path, name, pages, size)
        return self.q("SELECT * FROM docs WHERE path=?", path)[0]

    def doc_by_id(self, doc_id):
        return self.q("SELECT * FROM docs WHERE id=?", doc_id)[0]

    def ensure_tiles(self, doc_id, page, grid, model):
        with self.lock:
            self.db.execute("BEGIN")
            for idx, (x, y, w, h) in enumerate(grid):
                self.db.execute(
                    "INSERT OR IGNORE INTO tiles(doc_id, page, idx, x, y, w, h, model) VALUES(?,?,?,?,?,?,?,?)",
                    (doc_id, page, idx, x, y, w, h, model))
            self.db.execute("COMMIT")

    def tile_done(self, tile_id, hits, raw, in_tok, out_tok):
        with self.lock:
            self.db.execute("BEGIN")
            self.db.execute("DELETE FROM hits WHERE tile_id=?", (tile_id,))
            for h in hits:
                self.db.execute(
                    "INSERT INTO hits(tile_id, target, word, quote, certainty, note) VALUES(?,?,?,?,?,?)",
                    (tile_id, h["target"], h["word"], h["quote"], h["certainty"], h.get("note", "")))
            self.db.execute(
                "UPDATE tiles SET status='done', response=?, error=NULL, in_tok=?, out_tok=?, updated=? WHERE id=?",
                (raw, in_tok, out_tok, now(), tile_id))
            self.db.execute("COMMIT")

    def tile_error(self, tile_id, error, status="error"):
        self.x("UPDATE tiles SET status=?, error=?, batch_id=NULL, updated=? WHERE id=?",
               status, str(error)[:1000], now(), tile_id)

    def save_candidates(self, doc_id, model, clusters):
        """Перестраивает кандидатов документа; уже проверенные сохраняются."""
        keep = set()
        with self.lock:
            self.db.execute("BEGIN")
            for c in clusters:
                x, y, w, h = c["rect"]
                self.db.execute(
                    "INSERT OR IGNORE INTO candidates(doc_id, page, model, target, x, y, w, h, word, quote, certainty, tiles)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (doc_id, c["page"], model, c["target"], x, y, w, h, c["word"], c["quote"], c["certainty"],
                     json.dumps(c["tiles"])))
                self.db.execute(
                    "UPDATE candidates SET word=?, quote=?, certainty=?, tiles=? WHERE doc_id=? AND page=? AND model=?"
                    " AND target=? AND x=? AND y=? AND w=? AND h=?",
                    (c["word"], c["quote"], c["certainty"], json.dumps(c["tiles"]),
                     doc_id, c["page"], model, c["target"], x, y, w, h))
                keep.add((c["page"], c["target"], x, y, w, h))
            for r in self.db.execute("SELECT id, page, target, x, y, w, h FROM candidates WHERE doc_id=? AND model=?"
                                     " AND status='new'", (doc_id, model)).fetchall():
                if (r["page"], r["target"], r["x"], r["y"], r["w"], r["h"]) not in keep:
                    self.db.execute("DELETE FROM candidates WHERE id=?", (r["id"],))
            self.db.execute("COMMIT")
