"""Resolve a source URL to one-or-more downloadable media files.

Supports:
  - bunkr.cr / bunkr.si / bunkrr albums and single-file pages
  - direct media URLs (.mp4 .webm .mov .jpg .jpeg .png .webp .gif)
  - everything else falls back to yt-dlp (handles YT, TikTok, IG, Reddit, etc.)
"""
import os, re, time, uuid, mimetypes, subprocess, shutil
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup
from . import config, db

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept": "*/*"}
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
VID_EXT = {".mp4", ".webm", ".mov", ".mkv", ".m4v"}

def _ext(url):
    path = urlparse(url).path.lower()
    for e in IMG_EXT | VID_EXT:
        if path.endswith(e): return e
    return ""

def media_type_of(path_or_url):
    e = _ext(path_or_url)
    if e in VID_EXT: return "video"
    if e in IMG_EXT: return "image"
    mt, _ = mimetypes.guess_type(path_or_url)
    if mt:
        if mt.startswith("video"): return "video"
        if mt.startswith("image"): return "image"
    return None

# ---------- bunkr ----------
BUNKR_HOSTS = ("bunkr.", "bunkrr.")

def _is_bunkr(url):
    h = urlparse(url).hostname or ""
    return any(b in h for b in BUNKR_HOSTS)

def _gallery_dl_urls(url, timeout=120):
    """Return list of direct CDN URLs using gallery-dl (-g = print URLs only).

    gallery-dl sometimes exits non-zero even when stdout contains valid URLs
    (e.g. some sub-extractor failed). We treat any URL on stdout as success."""
    try:
        p = subprocess.run(
            ["gallery-dl", "-q", "-g", url],
            capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError("gallery-dl not installed. pip install gallery-dl")
    text = (p.stdout or "") + "\n" + (p.stderr or "")
    urls = [ln.strip() for ln in text.splitlines()
            if ln.strip().startswith("http")
            and re.search(r'\.(mp4|m4v|webm|mov|mkv|jpg|jpeg|png|webp|gif)(\?|$)', ln, re.I)]
    if urls:
        return urls
    if p.returncode != 0:
        raise RuntimeError(f"gallery-dl failed (rc={p.returncode}): {(p.stderr or p.stdout)[-300:]}")
    return []

def _fetch_album_page(page_url):
    """Single-page HTML fetch with patient retries. Returns set of file-page URLs.

    bunkr aggressively 5xx's rapid pagination so we back off generously."""
    base = "{0.scheme}://{0.netloc}".format(urlparse(page_url))
    backoffs = [5, 15, 30, 60, 90]
    for attempt, wait in enumerate(backoffs):
        try:
            r = requests.get(page_url, headers=HEADERS, timeout=30)
            if r.status_code == 404:
                return set()
            if r.status_code >= 500 or r.status_code == 429:
                raise requests.HTTPError(f"{r.status_code}")
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            return {urljoin(base, a["href"]) for a in soup.find_all("a", href=True)
                    if re.search(r"/[fvid]/[^/?#]+$", a["href"])}
        except Exception as e:
            db.log("WARN", f"album page attempt {attempt+1} failed ({page_url}): {e} — sleeping {wait}s")
            time.sleep(wait)
    return None  # signal: unrecoverable

def _bunkr_album_files(album_url, max_pages=50):
    """Walk every page of a bunkr album (?page=1, 2, ...) until no new files.

    Returns combined sorted list of all file-PAGE URLs. Per-file CDN resolution
    happens later in download() via gallery-dl."""
    base = album_url.split("?")[0]
    seen = set()
    for page in range(1, max_pages + 1):
        url = base if page == 1 else f"{base}?page={page}"
        files = _fetch_album_page(url)
        if files is None:
            db.log("ERROR", f"album page {page} unreachable; stopping enumeration")
            break
        new = files - seen
        if not new:
            db.log("INFO", f"album: no new files on page {page} — done ({len(seen)} total)")
            break
        seen |= new
        db.log("INFO", f"album page {page}: +{len(new)} (total {len(seen)})")
        if page >= 1:
            time.sleep(3)  # polite delay between pages — bunkr is touchy
    if seen:
        return sorted(seen)
    # Last-resort fallback: gallery-dl enumerate
    db.log("WARN", "HTML pagination yielded 0 files — falling back to gallery-dl")
    return _gallery_dl_urls(album_url, timeout=900)

_MEDIA_RE = re.compile(
    r'https?://[^\s"\'<>]+?\.(?:mp4|m4v|webm|mov|mkv|jpg|jpeg|png|webp|gif)(?:\?[^\s"\'<>]*)?',
    re.I)

def _score_cdn(u):
    """Lower score = more likely to be the real file (not a thumbnail)."""
    s = 0
    ul = u.lower()
    if "/thumbs/" in ul or "/thumb/" in ul: s += 100
    if "scdn.st" in ul:                     s += 50   # bunkr's thumbnail CDN
    if "preview" in ul:                     s += 40
    # video extensions preferred for "unknown" files
    if re.search(r'\.(mp4|webm|mov|mkv|m4v)(\?|$)', ul): s -= 10
    return s

def _bunkr_resolve_file(page_url):
    """Resolve a bunkr file page to its direct CDN URL.

    Strategy:
      0. gallery-dl (preferred — handles bunkr's rotating encryption)
      1. og:video
      2. <source> / <video src=>
      3. Any non-thumbnail CDN URL, lowest score wins
      4. og:image (last resort — thumbnail)
    """
    # Try gallery-dl up to 3 times (handles 5xx / transient errors)
    for attempt in range(3):
        try:
            urls = _gallery_dl_urls(page_url)
            if urls:
                return urls[0]
        except Exception as e:
            db.log("WARN", f"gallery-dl attempt {attempt+1} failed: {e}")
            time.sleep(3 * (attempt + 1))
    # Fallback: HTML scrape with retries
    html = None
    for attempt in range(4):
        try:
            r = requests.get(page_url, headers=HEADERS, timeout=30)
            if r.status_code >= 500 or r.status_code == 429:
                raise requests.HTTPError(f"{r.status_code}")
            r.raise_for_status()
            html = r.text
            break
        except Exception as e:
            db.log("WARN", f"HTML attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)
    if html is None:
        raise RuntimeError(f"bunkr file unreachable (likely rate-limited): {page_url}")

    # 1) og:video is always the real video
    m = re.search(r'<meta[^>]+property=["\']og:video(?::secure_url|:url)?["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
    if m: return m.group(1)

    # 2) <source> / <video src=>
    m = re.search(r'<(?:source|video)[^>]+src=["\']([^"\']+)["\']', html, re.I)
    if m and _score_cdn(m.group(1)) < 50:
        return m.group(1)

    # 3) Collect every media URL, pick the best (non-thumbnail) one
    candidates = list({u for u in _MEDIA_RE.findall(html)})
    if candidates:
        candidates.sort(key=_score_cdn)
        best = candidates[0]
        if _score_cdn(best) < 50:  # found a real one
            return best

    # 4) Last resort — og:image (thumbnail)
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
    if m:
        db.log("WARN", f"bunkr: only thumbnail found for {page_url} — likely HTML changed")
        return m.group(1)

    # Save HTML for diagnosis
    dump = config.DATA_DIR / "bunkr_debug.html"
    dump.write_text(html, encoding="utf-8", errors="ignore")
    raise RuntimeError(f"Could not resolve bunkr file URL: {page_url} (HTML saved to {dump})")

def expand_source(url):
    """Given a single source URL, return list of dicts: [{media_url, media_type}].

    For bunkr albums we return FILE-PAGE URLs (fast, no resolution yet).
    Per-file CDN resolution happens lazily inside download()."""
    url = url.strip()
    if _is_bunkr(url):
        if "/a/" in url:
            files = _bunkr_album_files(url)
            db.log("INFO", f"bunkr album: {len(files)} files in {url}")
            return [{"media_url": f, "media_type": None} for f in files]
        else:
            return [{"media_url": url, "media_type": None}]
    # direct media
    mt = media_type_of(url)
    if mt:
        return [{"media_url": url, "media_type": mt}]
    # fallback: assume yt-dlp will handle on download
    return [{"media_url": url, "media_type": None}]

# ---------- download ----------
def _safe_name(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80]

def _ytdlp_download(url, dest_dir):
    out_tmpl = str(dest_dir / f"{uuid.uuid4().hex}.%(ext)s")
    cmd = ["yt-dlp", "-q", "--no-warnings", "--no-playlist",
           "-f", "bv*+ba/b", "--merge-output-format", "mp4",
           "-o", out_tmpl, url]
    try:
        subprocess.run(cmd, check=True, timeout=900)
    except FileNotFoundError:
        raise RuntimeError("yt-dlp not installed. pip install yt-dlp")
    # find the newest file in dest_dir
    files = sorted(dest_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise RuntimeError("yt-dlp produced no file")
    return str(files[0])

def download(media_url, source_url=None):
    """Download to local disk, return absolute path."""
    config.DOWNLOADS.mkdir(parents=True, exist_ok=True)
    # Lazy-resolve bunkr file pages to their real CDN URL
    if _is_bunkr(media_url) and not _ext(media_url):
        try:
            resolved = _bunkr_resolve_file(media_url)
            db.log("INFO", f"bunkr resolved: {media_url} -> {resolved[:80]}")
            media_url = resolved
        except Exception as e:
            db.log("WARN", f"bunkr resolve failed, trying yt-dlp on page: {e}")
            return _ytdlp_download(source_url or media_url, config.DOWNLOADS)
    e = _ext(media_url)
    if e:
        fname = f"{uuid.uuid4().hex}{e}"
        dest = config.DOWNLOADS / fname
        # Per-host headers — bunkr CDN requires a bunkr Referer (hotlink protection)
        host = (urlparse(media_url).hostname or "").lower()
        hdrs = dict(HEADERS)
        if "scdn.st" in host or "bunkr" in host:
            hdrs["Referer"] = "https://bunkr.cr/"
            hdrs["Origin"]  = "https://bunkr.cr"
        last_err = None
        for attempt in range(4):
            try:
                with requests.get(media_url, headers=hdrs, stream=True, timeout=180) as r:
                    if r.status_code in (502, 503, 504, 429):
                        raise requests.HTTPError(f"{r.status_code} from CDN")
                    r.raise_for_status()
                    with open(dest, "wb") as f:
                        for chunk in r.iter_content(1024 * 256):
                            if chunk: f.write(chunk)
                return str(dest)
            except Exception as exc:
                last_err = exc
                db.log("WARN", f"download attempt {attempt+1} failed: {exc}")
                time.sleep(2 ** attempt)
        raise RuntimeError(f"download failed after retries: {last_err}")
    # unknown -> yt-dlp
    return _ytdlp_download(source_url or media_url, config.DOWNLOADS)
