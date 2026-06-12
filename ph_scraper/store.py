"""SQLite 持久化：产品库（按 post id upsert 去重）+ 抓取任务记录 + 断点续传。"""
import json
import sqlite3
import time
from dataclasses import asdict

from .paths import checkpoint_file, db_file
from .scraper import Product

_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    post_id      TEXT PRIMARY KEY,
    name         TEXT, tagline TEXT, keywords TEXT, category TEXT,
    ph_url       TEXT, real_url TEXT,
    votes        INTEGER, comments INTEGER,
    daily_rank   TEXT, weekly_rank TEXT, monthly_rank TEXT,
    featured_at  TEXT, product_slug TEXT, post_slug TEXT,
    shortened_url TEXT, thumbnail TEXT,
    updated_at   REAL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period TEXT, label TEXT, started_at REAL, finished_at REAL,
    count INTEGER, status TEXT
);
"""


class Store:
    def __init__(self, path: str | None = None):
        self.path = path or str(db_file())
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---------- 产品 ----------

    def upsert_products(self, products: list[Product]):
        now = time.time()
        rows = []
        for p in products:
            pid = p.post_id or p.post_slug or p.shortened_url or p.name
            rows.append((pid, p.name, p.tagline, p.keywords, p.category,
                         p.ph_url, p.real_url, p.votes, p.comments,
                         p.daily_rank, p.weekly_rank, p.monthly_rank,
                         p.featured_at, p.product_slug, p.post_slug,
                         p.shortened_url, p.thumbnail, now))
        self.conn.executemany("""
            INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(post_id) DO UPDATE SET
              name=excluded.name, tagline=excluded.tagline, keywords=excluded.keywords,
              category=excluded.category, ph_url=excluded.ph_url,
              real_url=CASE WHEN excluded.real_url != '' THEN excluded.real_url ELSE real_url END,
              votes=excluded.votes, comments=excluded.comments,
              daily_rank=excluded.daily_rank, weekly_rank=excluded.weekly_rank,
              monthly_rank=excluded.monthly_rank, featured_at=excluded.featured_at,
              product_slug=excluded.product_slug, post_slug=excluded.post_slug,
              shortened_url=excluded.shortened_url, thumbnail=excluded.thumbnail,
              updated_at=excluded.updated_at
        """, rows)
        self.conn.commit()

    def known_real_urls(self) -> dict[str, str]:
        """post_id -> real_url，供增量抓取跳过已解析的产品。"""
        cur = self.conn.execute(
            "SELECT post_id, real_url FROM products WHERE real_url != ''")
        return dict(cur.fetchall())

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]

    # ---------- 任务 ----------

    def task_start(self, period: str, label: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO tasks(period,label,started_at,status) VALUES(?,?,?,'running')",
            (period, label, time.time()))
        self.conn.commit()
        return cur.lastrowid

    def task_finish(self, task_id: int, count: int, status: str = "done"):
        self.conn.execute(
            "UPDATE tasks SET finished_at=?, count=?, status=? WHERE id=?",
            (time.time(), count, status, task_id))
        self.conn.commit()


# ---------- 断点续传 ----------

def save_checkpoint(task_key: str, cursor: str | None, products: list[Product]):
    checkpoint_file(task_key).write_text(json.dumps({
        "cursor": cursor,
        "products": [asdict(p) for p in products],
        "saved_at": time.time(),
    }, ensure_ascii=False), encoding="utf-8")


def load_checkpoint(task_key: str) -> dict | None:
    p = checkpoint_file(task_key)
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        d["products"] = [Product(**x) for x in d.get("products", [])]
        return d
    except Exception:
        return None


def clear_checkpoint(task_key: str):
    checkpoint_file(task_key).unlink(missing_ok=True)
