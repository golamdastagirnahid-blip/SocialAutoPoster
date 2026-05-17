"""One-shot CLI runner used by GitHub Actions.

Usage:
    python -m automation.cli tick [--force]
    python -m automation.cli post-once <facebook|youtube> [--force]
    python -m automation.cli sync-sources

`tick` is the entry point the cron workflow calls. It:
  1. Reads sources/{facebook,youtube}.txt and adds new bunkr-album files to queue
  2. Decides for each platform whether to post NOW (humanized random)
  3. If yes → pops one queue item → resolve → download → AI → upload → record
  4. Saves state.json (which workflow then commits back to the repo)

The decision rule is intentionally simple and unpredictable:
  - We aim for ~1 post / 24 h per platform.
  - If <20 h since last post → never post (cooldown).
  - If 20-30 h since last post → 30 % chance per cron tick.
  - If >30 h since last post → 90 % chance (catch-up).
  - --force always posts.
"""
import os, sys, random, traceback, argparse, time
from pathlib import Path
from . import config, state, scraper, ai, facebook, youtube

PLATFORMS = ("facebook", "youtube")

# ---------- sources ----------
def _read_source_file(path):
    if not path.exists(): return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out

def sync_sources(st):
    """Read sources/*.txt, expand any new ones into the per-platform queue."""
    repo_root = Path(__file__).resolve().parent.parent
    for plat in PLATFORMS:
        srcs = _read_source_file(repo_root / "sources" / f"{plat}.txt")
        for src in srcs:
            try:
                expanded = scraper.expand_source(src)
            except Exception as e:
                print(f"[WARN] expand {src} failed: {e}")
                continue
            added = 0
            for item in expanded:
                if state.is_seen(st, plat, item["media_url"]):
                    continue
                # don't duplicate within queue either
                if any(q["media_url"] == item["media_url"] for q in st[plat]["queue"]):
                    continue
                state.queue_push(st, plat, {
                    "source_url": src,
                    "media_url": item["media_url"],
                    "media_type": item.get("media_type"),
                    "added_ts": int(time.time()),
                })
                added += 1
            if added:
                print(f"[INFO] {plat}: +{added} new file(s) from {src}")

# ---------- decision ----------
def _should_post_now(st, platform, force=False):
    if force: return True, "forced"
    p = st[platform]
    if p.get("paused"):
        return False, f"{platform} is paused"
    if not p["queue"]:
        return False, "queue empty"
    h = state.hours_since_last(st, platform)
    if h < 20:
        return False, f"cooldown ({h:.1f}h since last post)"
    if h < 30:
        # 30% per tick — over a 10-h window with hourly cron => ~96% cumulative
        if random.random() < 0.30:
            return True, f"random ({h:.1f}h)"
        return False, f"random skip ({h:.1f}h)"
    # >30h: catch up
    if random.random() < 0.90:
        return True, f"catch-up ({h:.1f}h)"
    return False, f"catch-up skip ({h:.1f}h)"

# ---------- post one ----------
def post_once(st, platform, force=False):
    ok, reason = _should_post_now(st, platform, force=force)
    print(f"[{platform}] decision: {'POST' if ok else 'SKIP'} ({reason})")
    if not ok: return
    item = state.queue_pop_next(st, platform)
    if not item:
        print(f"[{platform}] queue empty after pop?")
        return
    url = item["media_url"]
    state.add_seen(st, platform, url)
    try:
        local = scraper.download(url, item.get("source_url"))
        mt = item.get("media_type") or scraper.media_type_of(local) or "video"
        if platform == "youtube" and mt != "video":
            print(f"[{platform}] skipping non-video for YouTube: {url}")
            return
        meta = ai.generate(platform, filename_hint=os.path.basename(local))
        mod = facebook if platform == "facebook" else youtube
        rid = mod.upload(local, mt, meta["title"], meta["description"], meta["tags"])
        state.record_post(st, platform, remote_id=rid,
                          title=meta["title"], media_url=url)
        print(f"[{platform}] POSTED id={rid} title={meta['title']!r}")
        # tidy up the downloaded file
        try: Path(local).unlink(missing_ok=True)
        except: pass
    except Exception as e:
        print(f"[{platform}] FAILED: {e}")
        traceback.print_exc()
        state.record_failure(st, platform, e)
        # Re-queue at the front so we retry next tick
        st[platform]["queue"].insert(0, item)

# ---------- entry ----------
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync-sources")
    t = sub.add_parser("tick");      t.add_argument("--force", action="store_true")
    p = sub.add_parser("post-once"); p.add_argument("platform", choices=PLATFORMS); p.add_argument("--force", action="store_true")
    args = ap.parse_args()

    st = state.load()

    if args.cmd == "sync-sources":
        sync_sources(st)
    elif args.cmd == "tick":
        sync_sources(st)
        for plat in PLATFORMS:
            post_once(st, plat, force=args.force)
    elif args.cmd == "post-once":
        sync_sources(st)
        post_once(st, args.platform, force=args.force)

    state.save(st)
    print(f"[STATE] saved -> {state.STATE_FILE}")

if __name__ == "__main__":
    main()
