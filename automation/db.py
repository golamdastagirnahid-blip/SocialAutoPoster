"""SQLite-backed queue, history & logs. Thread-safe via short-lived connections."""
import sqlite3, time, json, threading
from contextlib import contextmanager
from . import config

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,         -- 'facebook' | 'youtube'
    source_url TEXT NOT NULL,
    media_url TEXT,                 -- resolved direct URL
    local_path TEXT,
    media_type TEXT,                -- 'video' | 'image'
    status TEXT NOT NULL DEFAULT 'pending', -- pending|downloading|ready|posting|posted|failed|skipped
    title TEXT, description TEXT, tags TEXT,
    scheduled_at INTEGER,           -- unix epoch
    posted_at INTEGER,
    remote_id TEXT,
    error TEXT,
    attempts INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_q_status   ON queue(status);
CREATE INDEX IF NOT EXISTS idx_q_platform ON queue(platform);
CREATE INDEX IF NOT EXISTS idx_q_sched    ON queue(scheduled_at);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    level TEXT NOT NULL,
    msg TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen (
    source_url TEXT PRIMARY KEY,
    platform TEXT,
    ts INTEGER
);
CREATE TABLE IF NOT EXISTS platform_state (
    platform TEXT PRIMARY KEY,
    paused INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_error_ts INTEGER
);
INSERT OR IGNORE INTO platform_state(platform) VALUES ('facebook');
INSERT OR IGNORE INTO platform_state(platform) VALUES ('youtube');
"""

PAUSE_AFTER_FAILS = 5
MAX_ATTEMPTS = 3

@contextmanager
def conn():
    with _lock:
        c = sqlite3.connect(config.DB_FILE, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

def init():
    with conn() as c:
        c.executescript(SCHEMA)

# auto-init when module is imported so ad-hoc scripts work too
try:
    init()
except Exception as _e:
    print("db auto-init failed:", _e)

def log(level, msg):
    print(f"[{level}] {msg}")
    try:
        with conn() as c:
            c.execute("INSERT INTO logs(ts,level,msg) VALUES(?,?,?)", (int(time.time()), level, str(msg)[:4000]))
            c.execute("DELETE FROM logs WHERE id IN (SELECT id FROM logs ORDER BY id DESC LIMIT -1 OFFSET 2000)")
    except Exception as e:
        print("log fail:", e)

def add_jobs(platform, urls):
    """Add user-supplied source URLs as pending jobs.

    NOTE: We deliberately do NOT mark these in `seen` here, because for albums
    the user may legitimately re-paste the same album URL later to pick up
    additional pages/files that failed the first time. Per-file dedup happens
    when the worker fans out the album in fanout_seen()."""
    added = 0
    now = int(time.time())
    with conn() as c:
        for u in urls:
            u = u.strip()
            if not u: continue
            # Allow the same album URL to be re-added (worker dedups per file)
            c.execute("INSERT INTO queue(platform,source_url,status,created_at) VALUES(?,?,?,?)",
                      (platform, u, 'pending', now))
            added += 1
    return added

def is_file_seen(platform, file_url):
    with conn() as c:
        return c.execute("SELECT 1 FROM seen WHERE source_url=? AND platform=?",
                         (file_url, platform)).fetchone() is not None

def mark_file_seen(platform, file_url):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO seen(source_url,platform,ts) VALUES(?,?,?)",
                  (file_url, platform, int(time.time())))

def get_jobs(status=None, platform=None, limit=200):
    q = "SELECT * FROM queue"
    cond, args = [], []
    if status:   cond.append("status=?");   args.append(status)
    if platform: cond.append("platform=?"); args.append(platform)
    if cond: q += " WHERE " + " AND ".join(cond)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]

def update(job_id, **fields):
    if not fields: return
    keys = ",".join(f"{k}=?" for k in fields)
    args = list(fields.values()) + [job_id]
    with conn() as c:
        c.execute(f"UPDATE queue SET {keys} WHERE id=?", args)

def next_due(platform):
    now = int(time.time())
    with conn() as c:
        r = c.execute("""SELECT * FROM queue
            WHERE platform=? AND status='ready' AND (scheduled_at IS NULL OR scheduled_at<=?)
            ORDER BY COALESCE(scheduled_at,0) ASC, id ASC LIMIT 1""", (platform, now)).fetchone()
        return dict(r) if r else None

def pending_for_processing():
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM queue WHERE status='pending' ORDER BY id ASC LIMIT 5").fetchall()]

def stats():
    with conn() as c:
        out = {}
        for r in c.execute("SELECT platform,status,COUNT(*) n FROM queue GROUP BY platform,status"):
            out.setdefault(r["platform"], {})[r["status"]] = r["n"]
        return out

def recent_logs(n=300):
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (n,)).fetchall()]

def delete_job(job_id):
    with conn() as c:
        c.execute("DELETE FROM queue WHERE id=?", (job_id,))

def retry_job(job_id):
    with conn() as c:
        c.execute("UPDATE queue SET status='pending', error=NULL, attempts=0, scheduled_at=NULL WHERE id=?", (job_id,))

# ---- Platform pause / failure tracking ----
def get_platform_state(platform):
    with conn() as c:
        r = c.execute("SELECT * FROM platform_state WHERE platform=?", (platform,)).fetchone()
        return dict(r) if r else {"platform": platform, "paused": 0, "consecutive_failures": 0,
                                  "last_error": None, "last_error_ts": None}

def is_paused(platform):
    return bool(get_platform_state(platform).get("paused"))

def set_paused(platform, paused, reason=None):
    with conn() as c:
        c.execute("UPDATE platform_state SET paused=?, last_error=?, last_error_ts=? WHERE platform=?",
                  (1 if paused else 0, reason, int(time.time()) if reason else None, platform))

def record_post_success(platform):
    with conn() as c:
        c.execute("UPDATE platform_state SET consecutive_failures=0, last_error=NULL WHERE platform=?",
                  (platform,))

def record_post_failure(platform, err):
    with conn() as c:
        c.execute("""UPDATE platform_state
                     SET consecutive_failures=consecutive_failures+1,
                         last_error=?, last_error_ts=?
                     WHERE platform=?""", (str(err)[:500], int(time.time()), platform))
        n = c.execute("SELECT consecutive_failures FROM platform_state WHERE platform=?",
                      (platform,)).fetchone()["consecutive_failures"]
    if n >= PAUSE_AFTER_FAILS:
        set_paused(platform, True, f"auto-paused after {n} consecutive failures: {err}")
        log("ERROR", f"{platform} AUTO-PAUSED after {n} failures: {err}")
    return n
