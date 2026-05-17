"""Pre-upload validation. Catches problems BEFORE we burn API quota.

Returns (ok: bool, reason: str|None, info: dict).
Uses ffprobe (bundled with ffmpeg) when available for duration/aspect; otherwise
falls back to size-only checks.
"""
import os, json, shutil, subprocess
from . import db

# Hard limits per platform (conservative — actual API limits are higher)
LIMITS = {
    "facebook": {
        "video": {"max_mb": 4096, "max_sec": 14400, "min_sec": 1},   # 4 GB / 4 hours
        "image": {"max_mb": 30,                       "min_sec": 0}, # 30 MB
        "reel":  {"max_mb": 1024, "max_sec": 90,      "min_sec": 3, "aspect": (9, 16)},
    },
    "youtube": {
        "video": {"max_mb": 256000, "max_sec": 43200, "min_sec": 1}, # well over Shorts limit
        "short": {"max_mb": 256,    "max_sec": 60,    "min_sec": 1, "aspect": (9, 16)},
    },
}

def _ffprobe(path):
    """Return {duration, width, height} or None if ffprobe unavailable / failed."""
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", path
        ], text=True, timeout=30)
        data = json.loads(out)
        v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
        if not v: return None
        return {
            "duration": float(data.get("format", {}).get("duration", 0)),
            "width": int(v.get("width", 0)),
            "height": int(v.get("height", 0)),
        }
    except Exception as e:
        db.log("WARN", f"ffprobe failed on {path}: {e}")
        return None

def _is_vertical(info):
    return info["height"] > info["width"]

def validate(local_path, platform, media_type):
    """Validate before upload. Returns (ok, reason, info)."""
    if not local_path or not os.path.exists(local_path):
        return False, "file missing on disk", {}
    size_mb = os.path.getsize(local_path) / (1024 * 1024)
    if size_mb < 0.01:
        return False, "file is empty (<10 KB)", {"size_mb": size_mb}

    # Determine which slot's limits apply
    if platform == "youtube":
        # Always treated as Short by our YT module
        slot = "short" if media_type == "video" else None
    else:  # facebook
        if media_type == "image":
            slot = "image"
        else:
            # We'll classify reel vs video based on ffprobe; default to video
            slot = "video"

    if slot is None:
        return False, f"{platform} does not accept {media_type}", {"size_mb": size_mb}

    limits = LIMITS[platform][slot]
    info = {"size_mb": round(size_mb, 2), "slot": slot}

    if size_mb > limits["max_mb"]:
        return False, f"{size_mb:.1f}MB exceeds {slot} limit {limits['max_mb']}MB", info

    # Duration / aspect via ffprobe (optional)
    if media_type == "video":
        probe = _ffprobe(local_path)
        if probe:
            info.update(probe)
            dur = probe["duration"]
            if dur < limits.get("min_sec", 0):
                return False, f"video too short ({dur:.1f}s)", info
            if "max_sec" in limits and dur > limits["max_sec"]:
                return False, f"video too long ({dur:.1f}s > {limits['max_sec']}s)", info
            # YT short / FB reel: prefer vertical; warn but don't block if not
            if slot in ("short", "reel") and probe["width"] and probe["height"]:
                if not _is_vertical(probe):
                    db.log("WARN", f"{slot} aspect not vertical "
                                   f"({probe['width']}x{probe['height']}) — reach may suffer")
            # FB: auto-promote to reel if vertical & short enough
            if platform == "facebook" and slot == "video" and probe["width"] and probe["height"]:
                if _is_vertical(probe) and dur <= LIMITS["facebook"]["reel"]["max_sec"]:
                    info["slot"] = "reel"
        else:
            info["probe"] = "skipped (ffprobe not installed)"

    return True, None, info
