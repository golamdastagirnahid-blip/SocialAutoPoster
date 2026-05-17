"""Portable JSON state store — used by the GitHub Actions runner.

The whole bot state lives in ONE file, `state.json`, committed back to the repo
after each run so the next cron tick can read where we left off.

Schema:
{
  "facebook": {
      "last_post_ts": 1715900000,
      "posted_count": 12,
      "queue":  [ {"source_url":..., "media_url":..., "added_ts":...}, ... ],
      "seen":   ["url1", "url2", ...],   # per-file dedup
      "history":[ {"ts":..., "remote_id":..., "title":..., "media_url":...} ],
      "paused": false,
      "consecutive_failures": 0,
      "last_error": null
  },
  "youtube": { ...same shape... }
}
"""
import json, os, time, threading
from pathlib import Path
from . import config

STATE_FILE = config.DATA_DIR / "state.json"
_LOCK = threading.Lock()

DEFAULT = lambda: {
    "facebook": {"last_post_ts": 0, "posted_count": 0, "queue": [], "seen": [],
                 "history": [], "paused": False, "consecutive_failures": 0, "last_error": None},
    "youtube":  {"last_post_ts": 0, "posted_count": 0, "queue": [], "seen": [],
                 "history": [], "paused": False, "consecutive_failures": 0, "last_error": None},
}

def load():
    if not STATE_FILE.exists():
        return DEFAULT()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return DEFAULT()
    # Merge with defaults to forward-fix missing keys
    base = DEFAULT()
    for plat in ("facebook", "youtube"):
        base[plat].update(data.get(plat, {}))
    return base

def save(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=False)
    os.replace(tmp, STATE_FILE)

# ----- helpers used by the runner -----
def add_seen(state, platform, url):
    s = state[platform]["seen"]
    if url not in s:
        s.append(url)
        # cap at 5000 most recent to keep file size sane
        if len(s) > 5000:
            del s[:-5000]

def is_seen(state, platform, url):
    return url in state[platform]["seen"]

def queue_push(state, platform, item):
    state[platform]["queue"].append(item)

def queue_pop_next(state, platform):
    q = state[platform]["queue"]
    return q.pop(0) if q else None

def record_post(state, platform, *, remote_id, title, media_url):
    p = state[platform]
    p["last_post_ts"] = int(time.time())
    p["posted_count"] = int(p.get("posted_count", 0)) + 1
    p["consecutive_failures"] = 0
    p["last_error"] = None
    p["history"].append({
        "ts": int(time.time()), "remote_id": str(remote_id),
        "title": title, "media_url": media_url,
    })
    # cap history at 200 entries
    if len(p["history"]) > 200:
        p["history"] = p["history"][-200:]

def record_failure(state, platform, err, pause_after=5):
    p = state[platform]
    p["consecutive_failures"] = int(p.get("consecutive_failures", 0)) + 1
    p["last_error"] = str(err)[:500]
    if p["consecutive_failures"] >= pause_after:
        p["paused"] = True

def hours_since_last(state, platform):
    last = state[platform].get("last_post_ts") or 0
    if last == 0: return 1e9
    return (time.time() - last) / 3600.0
