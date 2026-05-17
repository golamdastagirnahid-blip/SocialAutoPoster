"""Background worker: resolves sources -> downloads -> AI metadata -> schedules -> posts.
Run within the dashboard process as a daemon thread.
"""
import os, time, random, threading, traceback
from datetime import datetime, timedelta
from pathlib import Path
from . import config, db, scraper, ai, facebook, youtube

STOP = threading.Event()

# ----------- helpers -----------
def _humanized_schedule(platform):
    """Spread N posts/day across active hours with jitter."""
    per_day = config.FB_PER_DAY if platform == "facebook" else config.YT_PER_DAY
    if per_day <= 0: per_day = 1
    now = datetime.now()
    hours_span = max(1, config.HOUR_END - config.HOUR_START)
    slot = hours_span / per_day
    # find an open slot today (or tomorrow if past)
    base = now.replace(hour=config.HOUR_START, minute=0, second=0, microsecond=0)
    if now.hour >= config.HOUR_END:
        base += timedelta(days=1)
    # find existing scheduled times for this platform >= base
    existing = []
    for j in db.get_jobs(status="ready", platform=platform, limit=1000):
        if j.get("scheduled_at"):
            existing.append(j["scheduled_at"])
    existing.sort()
    target = base
    for i in range(per_day * 7):  # search up to a week
        candidate = base + timedelta(hours=slot * (i + 1)) + timedelta(minutes=random.randint(-config.JITTER_MIN, config.JITTER_MIN))
        # at least 30 min apart from any existing
        if all(abs(candidate.timestamp() - e) > 1800 for e in existing):
            target = candidate
            break
    if target < now + timedelta(minutes=5):
        target = now + timedelta(minutes=random.randint(5, 25))
    return int(target.timestamp())

# ----------- pipeline -----------
def _process_pending(job):
    job_id = job["id"]
    try:
        db.update(job_id, status="downloading", attempts=job["attempts"] + 1)
        sources = scraper.expand_source(job["source_url"])
        if not sources:
            raise RuntimeError("no media extracted from source")
        # Per-file dedup: skip files already queued from a previous paste
        fresh = [s for s in sources if not db.is_file_seen(job["platform"], s["media_url"])]
        if not fresh:
            db.update(job_id, status="skipped", error=f"all {len(sources)} files already queued previously")
            return
        if len(fresh) < len(sources):
            db.log("INFO", f"album re-fetch: {len(sources)-len(fresh)} files already seen, "
                            f"{len(fresh)} new")
        # First fresh file becomes this job; rest are fan-out children
        first, rest = fresh[0], fresh[1:]
        db.mark_file_seen(job["platform"], first["media_url"])
        now_ts = int(time.time())
        for extra in rest:
            db.mark_file_seen(job["platform"], extra["media_url"])
            with db.conn() as c:
                c.execute("""INSERT INTO queue(platform,source_url,media_url,media_type,status,created_at)
                             VALUES(?,?,?,?,?,?)""",
                          (job["platform"], extra["media_url"], extra["media_url"],
                           extra["media_type"], "downloading", now_ts))
        # Now download first
        local = scraper.download(first["media_url"], job["source_url"])
        mt = first["media_type"] or scraper.media_type_of(local) or "video"
        # YouTube doesn't accept images -> skip
        if job["platform"] == "youtube" and mt != "video":
            db.update(job_id, status="skipped", media_url=first["media_url"], local_path=local,
                      media_type=mt, error="youtube section accepts videos only")
            return
        meta = ai.generate(job["platform"], filename_hint=os.path.basename(local))
        sched = _humanized_schedule(job["platform"])
        db.update(job_id, status="ready",
                  media_url=first["media_url"], local_path=local, media_type=mt,
                  title=meta["title"], description=meta["description"],
                  tags=",".join(meta["tags"]), scheduled_at=sched, error=None)
        db.log("INFO", f"job #{job_id} ready, scheduled at {datetime.fromtimestamp(sched)}")
    except Exception as e:
        db.log("ERROR", f"job #{job_id} prep failed: {e}\n{traceback.format_exc()[:500]}")
        db.update(job_id, status="failed", error=str(e)[:1000])

def _process_extra_downloading():
    """Album-fanout jobs that were inserted in 'downloading' need their own AI+schedule."""
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM queue WHERE status='downloading' AND local_path IS NULL LIMIT 5").fetchall()]
    for job in rows:
        try:
            local = scraper.download(job["media_url"], job["source_url"])
            mt = job["media_type"] or scraper.media_type_of(local) or "video"
            if job["platform"] == "youtube" and mt != "video":
                db.update(job["id"], status="skipped", local_path=local,
                          error="youtube section accepts videos only")
                continue
            meta = ai.generate(job["platform"], filename_hint=os.path.basename(local))
            sched = _humanized_schedule(job["platform"])
            db.update(job["id"], status="ready", local_path=local, media_type=mt,
                      title=meta["title"], description=meta["description"],
                      tags=",".join(meta["tags"]), scheduled_at=sched, error=None)
        except Exception as e:
            db.log("ERROR", f"album item #{job['id']} failed: {e}")
            db.update(job["id"], status="failed", error=str(e)[:1000])

def _post_due():
    for platform in ("facebook", "youtube"):
        if db.is_paused(platform):
            continue  # skip paused platforms; user must resume from dashboard
        job = db.next_due(platform)
        if not job: continue
        # Retry cap — give up after MAX_ATTEMPTS
        if (job.get("attempts") or 0) >= db.MAX_ATTEMPTS:
            db.update(job["id"], status="failed",
                      error=f"max attempts ({db.MAX_ATTEMPTS}) reached")
            db.log("WARN", f"job #{job['id']} gave up after {db.MAX_ATTEMPTS} attempts")
            continue
        try:
            db.update(job["id"], status="posting",
                      attempts=(job.get("attempts") or 0) + 1)
            tags = (job.get("tags") or "").split(",") if job.get("tags") else []
            tags = [t for t in tags if t]
            mod = facebook if platform == "facebook" else youtube
            rid = mod.upload(job["local_path"], job["media_type"], job["title"], job["description"], tags)
            db.update(job["id"], status="posted", posted_at=int(time.time()),
                      remote_id=str(rid), error=None)
            db.record_post_success(platform)
            try: Path(job["local_path"]).unlink(missing_ok=True)
            except: pass
        except Exception as e:
            err = str(e)[:1000]
            db.log("ERROR", f"post job #{job['id']} ({platform}) failed: {e}\n{traceback.format_exc()[:400]}")
            # Re-queue back to 'ready' so it retries later, unless we're at the cap
            new_status = "failed" if (job.get("attempts") or 0) + 1 >= db.MAX_ATTEMPTS else "ready"
            db.update(job["id"], status=new_status, error=err)
            db.record_post_failure(platform, err)

# ----------- loops -----------
def _loop():
    db.log("INFO", "worker started")
    while not STOP.is_set():
        try:
            for job in db.pending_for_processing():
                if STOP.is_set(): break
                _process_pending(job)
            _process_extra_downloading()
            _post_due()
        except Exception as e:
            db.log("ERROR", f"worker loop: {e}")
        STOP.wait(20)

_thread = None
def start():
    global _thread
    if _thread and _thread.is_alive(): return
    STOP.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="automation-worker")
    _thread.start()

def stop():
    STOP.set()
