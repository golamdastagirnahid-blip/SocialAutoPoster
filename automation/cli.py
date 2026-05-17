"""Bulletproof one-shot CLI runner used by GitHub Actions.

Subcommands:
    python -m automation.cli tick                # main hourly entry point
    python -m automation.cli post-once <plat>    # force one platform now
    python -m automation.cli sync-sources        # only refresh queue
    python -m automation.cli health              # self-test, no posts
    python -m automation.cli reset-platform <p>  # clear pause + failure count

Routing rules (NEW):
    VIDEO  -> Facebook (Reels/video) AND YouTube (Shorts)
    IMAGE  -> Facebook only

Humanized cadence:
    - Variable interval: 18-32 h between posts (mean ~24 h).
    - Active-hour gate (UTC): only post inside ACTIVE_HOUR_START..END.
    - 5 % chance of a "rest day" once cooldown elapses.
    - Random per-tick coin flip biased by hours-since-last-post.

Self-healing each tick:
    - Repair malformed state.json.
    - Clean orphan downloads.
    - Prune queue items older than 60 days.
    - Pre-flight token ping.
    - Disk-space guard.
    - Sanity filters (file size, video duration, empty).
"""
import os, sys, random, traceback, argparse, time, shutil, json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import config, state, scraper, ai, facebook, youtube

PLATFORMS = ("facebook", "youtube")

# Per-platform cadence (target posts/day baked into cooldown band)
CADENCE = {
    "facebook": {"min_h":  9.0, "max_h": 15.0, "rest_day_prob": 0.03},  # ~2 posts/day
    "youtube":  {"min_h": 18.0, "max_h": 32.0, "rest_day_prob": 0.05},  # ~1 post/day
}
ITEM_MAX_ATTEMPTS = 3         # drop an item after this many failed posts
QUEUE_MAX_AGE_D = 60          # prune items unused for 60 d
ORPHAN_MAX_AGE_H = 6          # delete downloads older than 6 h
MIN_FREE_DISK_MB = 500        # abort if less than this on tick
MAX_FILE_MB      = 500        # skip files bigger than this
MAX_VIDEO_SEC    = 180        # skip videos longer than this
PROCESS_TIMEOUT_MIN = 20      # bail if we've been running > this many minutes

_t0 = time.time()
def _elapsed_min(): return (time.time() - _t0) / 60.0

# --------------------------------------------------------------------- log
def log(level, msg):
    print(f"[{level}] {msg}", flush=True)

# --------------------------------------------------------------------- sources
LEGACY_FILES = {"facebook": "facebook.txt", "youtube": "youtube.txt"}
UNIFIED_FILE = "content.txt"

def _read_lines(path):
    if not path.exists(): return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out

def _routes_for(media_type):
    """Return list of platforms a file should be posted to."""
    if media_type == "image": return ["facebook"]
    return ["facebook", "youtube"]   # video (or unknown -> treat as video)

def sync_sources(st):
    """Read source files, expand new URLs into per-platform queues."""
    repo_root = Path(__file__).resolve().parent.parent

    # Collect (source_url, restrict_to_platforms_or_None) pairs
    pairs = []
    for url in _read_lines(repo_root / "sources" / UNIFIED_FILE):
        pairs.append((url, None))
    for plat, fname in LEGACY_FILES.items():
        for url in _read_lines(repo_root / "sources" / fname):
            pairs.append((url, [plat]))

    if not pairs:
        log("WARN", "no source URLs found - add some to sources/content.txt")
        return

    for src, restrict in pairs:
        try:
            expanded = scraper.expand_source(src)
        except Exception as e:
            log("WARN", f"expand failed for {src}: {e}")
            continue
        added_total = 0
        for item in expanded:
            mt = item.get("media_type") or "video"
            routes = _routes_for(mt)
            if restrict is not None:
                routes = [p for p in routes if p in restrict]
            for plat in routes:
                if state.is_seen(st, plat, item["media_url"]): continue
                if any(q["media_url"] == item["media_url"] for q in st[plat]["queue"]):
                    continue
                state.queue_push(st, plat, {
                    "source_url": src,
                    "media_url": item["media_url"],
                    "media_type": mt,
                    "added_ts": int(time.time()),
                })
                added_total += 1
        if added_total:
            log("INFO", f"queued +{added_total} item(s) from {src}")

# --------------------------------------------------------------------- decision
def _snap_to_active_hours(ts):
    """If ts falls outside ACTIVE_HOUR_START..END (UTC), shift it forward
    into the next valid window with random minutes/seconds for natural look."""
    s, e = config.HOUR_START, config.HOUR_END
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    h = dt.hour
    in_window = (s <= h <= e) if s <= e else (h >= s or h <= e)
    if in_window:
        return ts
    # Shift to next start-of-window
    if s <= e:
        if h < s:
            new = dt.replace(hour=s, minute=random.randint(0, 59),
                             second=random.randint(0, 59), microsecond=0)
        else:  # h > e
            new = (dt + timedelta(days=1)).replace(
                hour=s, minute=random.randint(0, 59),
                second=random.randint(0, 59), microsecond=0)
    else:  # window crosses midnight - rare; just advance 1h
        new = dt + timedelta(hours=1)
    return int(new.timestamp())

def schedule_next(st, platform, *, base_ts=None):
    """Pick a fresh random target time for the NEXT post on this platform.

    Called after every successful post (and on first run). The random draw
    happens here, so each scheduled time is independent of any previous one
    - genuinely different every cycle.
    """
    cad = CADENCE[platform]
    base = base_ts if base_ts is not None else (st[platform].get("last_post_ts") or int(time.time()))
    h_target = random.uniform(cad["min_h"], cad["max_h"])
    # Occasional rest-day: extend by a full day
    if random.random() < cad["rest_day_prob"]:
        h_target += 24.0
        log("INFO", f"{platform}: rest-day rolled, next post pushed to ~{h_target:.1f}h gap")
    target_ts = base + h_target * 3600
    target_ts = _snap_to_active_hours(int(target_ts))
    st[platform]["next_post_at"] = target_ts
    when = datetime.fromtimestamp(target_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log("INFO", f"{platform}: next post scheduled at {when} (~{h_target:.1f}h gap)")
    return target_ts

def _ensure_scheduled(st, platform):
    """Make sure next_post_at is set. Migrates legacy state."""
    p = st[platform]
    if p.get("next_post_at"):
        return
    if p.get("last_post_ts"):
        schedule_next(st, platform)
    else:
        # Never posted: schedule first post for the very next active-hour minute
        p["next_post_at"] = _snap_to_active_hours(int(time.time()))
        when = datetime.fromtimestamp(p["next_post_at"], tz=timezone.utc).strftime("%H:%M UTC")
        log("INFO", f"{platform}: first run, scheduled at {when}")

def _within_active_hours(now=None):
    h = (now or time.gmtime()).tm_hour
    s, e = config.HOUR_START, config.HOUR_END
    return (s <= h <= e) if s <= e else (h >= s or h <= e)

def _should_post_now(st, platform, force=False):
    p = st[platform]
    if p.get("paused"): return False, f"{platform} paused: {p.get('last_error') or 'manual'}"
    if not p["queue"]:  return False, "queue empty"
    if force:           return True, "forced"
    _ensure_scheduled(st, platform)
    npa = p["next_post_at"]
    now = int(time.time())
    if now < npa:
        wait_h = (npa - now) / 3600.0
        when = datetime.fromtimestamp(npa, tz=timezone.utc).strftime("%m-%d %H:%M UTC")
        return False, f"waiting until {when} (in {wait_h:.1f}h)"
    if not _within_active_hours():
        return False, f"scheduled time reached but outside active hours - will fire next valid hour"
    when = datetime.fromtimestamp(npa, tz=timezone.utc).strftime("%H:%M UTC")
    return True, f"scheduled time reached (target was {when})"

# --------------------------------------------------------------------- preflight
def _disk_free_mb():
    try: return shutil.disk_usage(str(config.DATA_DIR)).free / (1024 * 1024)
    except: return 1e9

def _preflight_facebook():
    """Quick token ping - fail fast if FB token is bad."""
    import requests
    try:
        r = requests.get(f"https://graph.facebook.com/v20.0/{config.FB_PAGE_ID}",
            params={"fields": "id,name", "access_token": config.FB_PAGE_TOKEN},
            timeout=15)
        if not r.ok:
            return False, f"FB pre-flight failed ({r.status_code}): {r.text[:200]} - check FB_PAGE_ACCESS_TOKEN secret"
        return True, None
    except Exception as e:
        return False, f"FB pre-flight exception: {e}"

def _preflight_youtube():
    try:
        from googleapiclient.discovery import build
        from google.oauth2.credentials import Credentials
        youtube._hydrate_from_env()
        if not config.YT_TOKEN_FILE.exists():
            return False, "YT token file missing - check YT_TOKEN_B64 secret"
        creds = Credentials.from_authorized_user_file(str(config.YT_TOKEN_FILE),
            ["https://www.googleapis.com/auth/youtube.upload",
             "https://www.googleapis.com/auth/youtube.readonly"])
        if not creds.valid:
            from google.auth.transport.requests import Request
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                config.YT_TOKEN_FILE.write_text(creds.to_json())
            else:
                return False, "YT credentials invalid and cannot refresh - regenerate YT_TOKEN_B64"
        return True, None
    except Exception as e:
        return False, f"YT pre-flight exception: {e}"

PREFLIGHT = {"facebook": _preflight_facebook, "youtube": _preflight_youtube}

# --------------------------------------------------------------------- sanity
def _sanity_ok(local_path, media_type):
    if not local_path or not os.path.exists(local_path):
        return False, "file missing on disk"
    sz_mb = os.path.getsize(local_path) / (1024 * 1024)
    if sz_mb < 0.01: return False, "file empty (<10 KB)"
    if sz_mb > MAX_FILE_MB: return False, f"file too big ({sz_mb:.1f} MB > {MAX_FILE_MB} MB)"
    if media_type == "video":
        from .validate import _ffprobe
        info = _ffprobe(local_path)
        if info and info["duration"] > MAX_VIDEO_SEC:
            return False, f"video too long ({info['duration']:.0f}s > {MAX_VIDEO_SEC}s)"
    return True, None

# --------------------------------------------------------------------- post one
def post_once(st, platform, force=False, dry=False):
    ok, reason = _should_post_now(st, platform, force=force)
    log(platform.upper(), f"decision: {'POST' if ok else 'SKIP'} ({reason})")
    if not ok: return

    # Pre-flight credentials
    pf_ok, pf_err = PREFLIGHT[platform]()
    if not pf_ok:
        log("ERROR", pf_err)
        state.record_failure(st, platform, pf_err)
        return

    item = state.queue_pop_next(st, platform)
    if not item:
        log(platform.upper(), "queue empty after pop")
        return
    url = item["media_url"]
    state.add_seen(st, platform, url)

    if dry:
        log(platform.upper(), f"DRY-RUN would post: {url}")
        # put it back so dry-run doesn't consume
        st[platform]["queue"].insert(0, item)
        return

    local = None
    try:
        local = scraper.download(url, item.get("source_url"))
        mt = item.get("media_type") or scraper.media_type_of(local) or "video"
        # Sanity gate before AI/upload (cheap)
        s_ok, s_err = _sanity_ok(local, mt)
        if not s_ok:
            log("WARN", f"sanity skip: {s_err} - {url}")
            return  # consumed; will not retry
        if platform == "youtube" and mt != "video":
            log(platform.upper(), f"skip non-video for YouTube: {url}")
            return

        meta = ai.generate(platform, filename_hint=os.path.basename(local))
        mod  = facebook if platform == "facebook" else youtube
        rid  = mod.upload(local, mt, meta["title"], meta["description"], meta["tags"])

        state.record_post(st, platform, remote_id=rid,
                          title=meta["title"], media_url=url)
        log(platform.upper(), f"POSTED id={rid} title={meta['title']!r}")
        # Roll fresh random schedule for the NEXT post on this platform
        schedule_next(st, platform)
    except Exception as e:
        msg = str(e)[:300]
        log("ERROR", f"{platform} post failed: {msg}")
        traceback.print_exc()
        state.record_failure(st, platform, msg)
        # Per-item retry cap: a permanently-broken file must not block the queue
        item["attempts"] = int(item.get("attempts", 0)) + 1
        if item["attempts"] >= ITEM_MAX_ATTEMPTS:
            log("WARN", f"{platform} dropping item after {item['attempts']} failed attempts: "
                        f"{item.get('media_url')!r} - last_error={msg}")
            # already in seen, so it won't be re-fetched. Move on.
        else:
            # Re-queue at BACK (not front) so we don't block the rest of the queue
            st[platform]["queue"].append(item)
            log("INFO", f"{platform} re-queued at end (attempt {item['attempts']}/{ITEM_MAX_ATTEMPTS})")
    finally:
        if local:
            try: Path(local).unlink(missing_ok=True)
            except: pass

# --------------------------------------------------------------------- self-heal
def self_heal(st):
    """Maintenance pass run every tick."""
    # 1. Disk-space guard
    free = _disk_free_mb()
    if free < MIN_FREE_DISK_MB:
        log("WARN", f"low disk space: {free:.0f} MB free (<{MIN_FREE_DISK_MB} MB)")
    # 2. Orphan downloads cleanup
    try:
        cutoff = time.time() - ORPHAN_MAX_AGE_H * 3600
        n = 0
        if config.DOWNLOADS.exists():
            for f in config.DOWNLOADS.iterdir():
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True); n += 1
        if n: log("INFO", f"cleaned {n} orphan download(s)")
    except Exception as e:
        log("WARN", f"orphan cleanup failed: {e}")
    # 3. Prune ancient queue items
    cutoff = time.time() - QUEUE_MAX_AGE_D * 86400
    for plat in PLATFORMS:
        before = len(st[plat]["queue"])
        st[plat]["queue"] = [q for q in st[plat]["queue"]
                             if q.get("added_ts", time.time()) > cutoff]
        dropped = before - len(st[plat]["queue"])
        if dropped:
            log("INFO", f"{plat}: pruned {dropped} stale queue item(s) (>{QUEUE_MAX_AGE_D}d)")
    # 4. Repair: ensure required keys exist (state.load already merges defaults)

# --------------------------------------------------------------------- health
def health(st):
    log("HEALTH", "running self-test (no posts)")
    sync_sources(st)
    self_heal(st)
    for plat in PLATFORMS:
        ok, err = PREFLIGHT[plat]()
        log("HEALTH", f"{plat} preflight: {'OK' if ok else 'FAIL - ' + (err or '?')}")
        _ensure_scheduled(st, plat)  # fill next_post_at if missing
        npa = st[plat].get("next_post_at") or 0
        when = datetime.fromtimestamp(npa, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if npa else "(not scheduled yet)"
        log("HEALTH", f"{plat} queue size: {len(st[plat]['queue'])}, "
                      f"last post: {state.hours_since_last(st, plat):.1f}h ago, "
                      f"next post: {when}, "
                      f"paused: {st[plat]['paused']}, "
                      f"fails: {st[plat]['consecutive_failures']}")
    log("HEALTH", f"disk free: {_disk_free_mb():.0f} MB")

# --------------------------------------------------------------------- entry
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync-sources")
    sub.add_parser("health")
    t = sub.add_parser("tick");      t.add_argument("--force", action="store_true"); t.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("post-once"); p.add_argument("platform", choices=PLATFORMS); p.add_argument("--force", action="store_true"); p.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("reset-platform"); r.add_argument("platform", choices=PLATFORMS)
    args = ap.parse_args()

    st = state.load()

    try:
        if args.cmd == "sync-sources":
            sync_sources(st)
        elif args.cmd == "health":
            health(st)
        elif args.cmd == "reset-platform":
            st[args.platform]["paused"] = False
            st[args.platform]["consecutive_failures"] = 0
            st[args.platform]["last_error"] = None
            log("INFO", f"{args.platform} reset")
        elif args.cmd == "tick":
            self_heal(st)
            sync_sources(st)
            for plat in PLATFORMS:
                if _elapsed_min() > PROCESS_TIMEOUT_MIN:
                    log("WARN", "tick timeout reached, deferring remaining platforms")
                    break
                post_once(st, plat, force=args.force, dry=args.dry_run)
        elif args.cmd == "post-once":
            self_heal(st)
            sync_sources(st)
            post_once(st, args.platform, force=args.force, dry=args.dry_run)
    finally:
        state.save(st)
        log("STATE", f"saved -> {state.STATE_FILE}")

if __name__ == "__main__":
    main()
