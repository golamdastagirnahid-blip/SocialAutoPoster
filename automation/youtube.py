"""YouTube Shorts upload via YouTube Data API v3 (OAuth desktop flow).

Two ways to authenticate:
  - LOCAL:  client_secret.json + token.json files on disk.
  - CI:     base64-encoded blobs in env vars
            YT_CLIENT_SECRET_B64 and YT_TOKEN_B64.
"""
import os, base64, time
from . import config, db

_SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
           "https://www.googleapis.com/auth/youtube.readonly"]

def _hydrate_from_env():
    """If env vars are set (GitHub Actions), write client_secret.json/token.json to disk."""
    cs = os.getenv("YT_CLIENT_SECRET_B64")
    tk = os.getenv("YT_TOKEN_B64")
    if cs and not config.YT_CLIENT_SECRETS.exists():
        config.YT_CLIENT_SECRETS.parent.mkdir(parents=True, exist_ok=True)
        config.YT_CLIENT_SECRETS.write_bytes(base64.b64decode(cs))
    if tk and not config.YT_TOKEN_FILE.exists():
        config.YT_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.YT_TOKEN_FILE.write_bytes(base64.b64decode(tk))

def _service():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    _hydrate_from_env()
    creds = None
    if config.YT_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(config.YT_TOKEN_FILE), _SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if os.getenv("CI"):
                raise RuntimeError("YT_TOKEN_B64 missing or invalid; run authorize once locally and copy token.json contents to GitHub Secret YT_TOKEN_B64 (base64).")
            if not config.YT_CLIENT_SECRETS.exists():
                raise RuntimeError(f"Missing {config.YT_CLIENT_SECRETS}. Create OAuth Desktop creds in Google Cloud Console.")
            flow = InstalledAppFlow.from_client_secrets_file(str(config.YT_CLIENT_SECRETS), _SCOPES)
            creds = flow.run_local_server(port=0)
        config.YT_TOKEN_FILE.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)

def authorize_interactive():
    """Run once to perform OAuth (opens browser)."""
    _service()
    return True

def upload(local_path, media_type, title, description, tags):
    if media_type != "video":
        raise RuntimeError("YouTube section is video/shorts only. Skip image jobs.")
    from .validate import validate
    ok, reason, info = validate(local_path, "youtube", media_type)
    if not ok:
        raise RuntimeError(f"validation failed: {reason}")
    db.log("INFO", f"youtube validate ok: {info}")
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError
    yt = _service()
    # Ensure #Shorts hint in title or description for vertical < 60s clips
    if "#shorts" not in (title + description).lower():
        description = description + "\n\n#Shorts"
    body = {
        "snippet": {
            "title": title[:95] or "New Short",
            "description": description[:4900],
            "tags": [t[:30] for t in (tags or [])][:25],
            "categoryId": config.YT_CATEGORY_ID,
        },
        "status": {"privacyStatus": config.YT_PRIVACY,
                   "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(local_path, chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/*")
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None; tries = 0
    while resp is None:
        try:
            status, resp = req.next_chunk()
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504) and tries < 5:
                tries += 1; time.sleep(2 ** tries); continue
            raise
    vid = resp["id"]
    db.log("INFO", f"youtube uploaded id={vid}")
    return vid
