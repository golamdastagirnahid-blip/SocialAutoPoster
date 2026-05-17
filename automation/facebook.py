"""Facebook Page uploads via Graph API.
- Reels:  3-step Resumable Upload to /{page_id}/video_reels (best reach)
- Videos: POST /{page_id}/videos  (resumable for >100MB)
- Photos: POST /{page_id}/photos
"""
import os, time, requests
from . import config, db
from .validate import validate

GRAPH = "https://graph.facebook.com/v20.0"

def _check():
    if not config.FB_PAGE_ID or not config.FB_PAGE_TOKEN:
        raise RuntimeError("FB_PAGE_ID / FB_PAGE_ACCESS_TOKEN not set in .env")

def post_photo(local_path, caption):
    _check()
    url = f"{GRAPH}/{config.FB_PAGE_ID}/photos"
    with open(local_path, "rb") as f:
        r = requests.post(url, data={"caption": caption, "access_token": config.FB_PAGE_TOKEN},
                          files={"source": f}, timeout=600)
    r.raise_for_status()
    return r.json().get("post_id") or r.json().get("id")

def post_video(local_path, title, description):
    _check()
    url = f"{GRAPH}/{config.FB_PAGE_ID}/videos"
    size = os.path.getsize(local_path)
    # Simple upload for <100MB; otherwise resumable session
    if size < 90 * 1024 * 1024:
        with open(local_path, "rb") as f:
            r = requests.post(url, data={
                "title": title, "description": description,
                "access_token": config.FB_PAGE_TOKEN,
            }, files={"source": f}, timeout=1800)
        r.raise_for_status()
        return r.json().get("id")
    # Resumable
    r = requests.post(url, data={
        "upload_phase": "start", "file_size": size,
        "access_token": config.FB_PAGE_TOKEN}, timeout=60)
    r.raise_for_status()
    j = r.json()
    upload_session_id = j["upload_session_id"]
    video_id = j["video_id"]
    start = int(j["start_offset"]); end = int(j["end_offset"])
    with open(local_path, "rb") as f:
        while start < end:
            f.seek(start)
            chunk = f.read(end - start)
            rr = requests.post(url, data={
                "upload_phase": "transfer", "upload_session_id": upload_session_id,
                "start_offset": start, "access_token": config.FB_PAGE_TOKEN,
            }, files={"video_file_chunk": chunk}, timeout=1800)
            rr.raise_for_status()
            jj = rr.json()
            start = int(jj["start_offset"]); end = int(jj["end_offset"])
            if start == end: break
    fin = requests.post(url, data={
        "upload_phase": "finish", "upload_session_id": upload_session_id,
        "title": title, "description": description,
        "access_token": config.FB_PAGE_TOKEN}, timeout=120)
    fin.raise_for_status()
    return video_id

def post_reel(local_path, description, max_wait=300):
    """3-step Reels upload: start session -> upload binary -> finish & publish.

    Reels get significantly better organic reach than /videos posts."""
    _check()
    size = os.path.getsize(local_path)
    # 1) Start upload session
    r = requests.post(f"{GRAPH}/{config.FB_PAGE_ID}/video_reels",
                      data={"upload_phase": "start", "access_token": config.FB_PAGE_TOKEN},
                      timeout=60)
    r.raise_for_status()
    j = r.json()
    video_id = j["video_id"]
    upload_url = j.get("upload_url") or f"https://rupload.facebook.com/video-upload/v20.0/{video_id}"
    # 2) Upload the binary in one shot
    with open(local_path, "rb") as f:
        rr = requests.post(upload_url,
            headers={"Authorization": f"OAuth {config.FB_PAGE_TOKEN}",
                     "offset": "0", "file_size": str(size)},
            data=f.read(), timeout=1800)
    rr.raise_for_status()
    # 3) Finish & publish
    fin = requests.post(f"{GRAPH}/{config.FB_PAGE_ID}/video_reels",
        params={"access_token": config.FB_PAGE_TOKEN},
        data={"video_id": video_id, "upload_phase": "finish",
              "video_state": "PUBLISHED", "description": description},
        timeout=120)
    fin.raise_for_status()
    # 4) Wait for processing (FB sometimes takes 30-90s)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        s = requests.get(f"{GRAPH}/{video_id}",
            params={"fields": "status", "access_token": config.FB_PAGE_TOKEN}, timeout=30)
        if s.ok:
            st = s.json().get("status", {})
            if st.get("video_status") in ("ready", "PUBLISHED"):
                break
            if st.get("video_status") == "error":
                raise RuntimeError(f"reel processing error: {st}")
        time.sleep(10)
    return video_id

def upload(local_path, media_type, title, description, tags):
    """Validate + route to correct endpoint (photo / reel / video)."""
    ok, reason, info = validate(local_path, "facebook", media_type)
    if not ok:
        raise RuntimeError(f"validation failed: {reason}")
    db.log("INFO", f"facebook validate ok: {info}")
    hashtags = " ".join("#" + t for t in tags)
    full_desc = f"{title}\n\n{description}\n\n{hashtags}".strip()

    slot = info.get("slot", "video")
    if media_type == "image":
        rid = post_photo(local_path, full_desc)
    elif slot == "reel":
        db.log("INFO", "facebook: routing to Reels endpoint (vertical, ≤90s)")
        rid = post_reel(local_path, full_desc)
    else:
        rid = post_video(local_path, title, full_desc)
    db.log("INFO", f"facebook posted id={rid} as {slot}")
    return rid
