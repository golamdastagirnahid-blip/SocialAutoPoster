# SocialAutoPoster

A self-dependent, monetization-safe automation tool that auto-posts media
from source links (bunkr albums, direct URLs, anything yt-dlp supports) to
your **Facebook Page** (Reels + photos + videos) and **YouTube Shorts** with
AI-written titles, descriptions, and hashtags on a humanized schedule.

## Features

- Two independent queues: Facebook (videos + images) and YouTube (videos only)
- bunkr.cr album expansion with full pagination + per-file deduplication
- gallery-dl based per-file resolver (handles bunkr's rotating obfuscation)
- Groq AI metadata with OpenRouter fallback and a safe template fallback
- Humanized scheduler: N posts/day spread across active hours with jitter
- Auto-routes vertical short videos to FB **Reels** for better reach
- Pre-upload validation (size, duration, aspect ratio via ffprobe)
- Auto-pause platform on consecutive failures (e.g. expired token)
- Retry cap (3) so failed jobs never loop forever
- Flask dashboard with pause/resume controls and live logs

## 1. Install
```powershell
cd C:\Users\golam\CascadeProjects\SocialAutoPoster
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Configure
```powershell
copy .env.example .env
notepad .env
```
Fill in:
- `GROQ_API_KEY` (free at https://console.groq.com)
- `OPENROUTER_API_KEY` (optional fallback, https://openrouter.ai)
- `FB_PAGE_ID` + `FB_PAGE_ACCESS_TOKEN` — see [docs/FACEBOOK_SETUP.md](docs/FACEBOOK_SETUP.md)
  for the **System User Access Token** flow (never expires, recommended for production).
- Place `client_secret.json` (Google Cloud Console → OAuth client → Desktop) next to `run.py`.

After setting `FB_PAGE_*`, verify the token is valid and has all required scopes:
```powershell
python -m automation.fb_check
```

## 3. Run
```powershell
python run.py
```
Open http://127.0.0.1:5000

- Paste source URLs (bunkr albums, direct .mp4/.jpg, or any yt-dlp-supported page)
  into the **Facebook** or **YouTube** box.
- Click **Authorize YouTube** once (opens browser for OAuth).
- The worker thread will:
  1. Expand album → resolve files → download
  2. Ask Groq (then OpenRouter) for title/desc/tags
  3. Schedule with humanized jitter inside `ACTIVE_HOUR_START..END`
  4. Auto-upload at the scheduled time
- Watch progress in the queue tables. Check **Logs** for details.

## Notes
- YouTube Data API allows ~6 uploads/day per project (10k unit quota).
  Raise `YT_POSTS_PER_DAY` only after requesting more quota.
- Facebook resumable upload is used for videos >90 MB.
- Vertical videos ≤90 s are auto-published as Reels for better reach.
- All state is in `automation_data/queue.db` — safe to delete to reset.
- Per-file dedup: re-pasting an album later only adds files not yet seen.
- Optional: install `ffmpeg` (provides `ffprobe`) for full video validation:
  `winget install ffmpeg`

## License
Personal-use project. Use responsibly and respect the source sites' ToS.
