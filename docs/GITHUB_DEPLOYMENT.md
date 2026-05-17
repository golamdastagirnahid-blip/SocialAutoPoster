# Run It Forever on GitHub Actions (Free)

This guide takes you from zero to a 24/7 auto-poster running on GitHub's free
servers. Your PC can be off. Posting decisions are humanized (random time
inside a 24-hour window).

> **You'll need:** about 30 minutes once. After that, adding new content is
> just editing a text file on github.com — no terminal, no setup.

---

## Overview of how it works

1. Every hour, GitHub runs your workflow on a free Ubuntu VM.
2. The workflow reads `sources/facebook.txt` and `sources/youtube.txt`,
   expands any new bunkr albums into a queue, then **decides randomly**
   whether to post each platform now (aiming for ~1 post / 24 h).
3. If it decides to post: pick the oldest queued file → download →
   AI title/description → upload → record success.
4. The updated state is committed back to the repo so the next hour's run
   knows where it left off. You can see post history right on github.com.
5. You can also click **Actions → Auto Post → Run workflow** any time to
   force-post immediately.

---

## Step 1 — Get all the credentials

You need **5 secrets**. Get each one and keep them in a notepad temporarily.

### 1.1 `GROQ_API_KEY`
1. Go to https://console.groq.com/keys → **Create API Key** → copy.
   *(free tier: 14,400 requests/day — way more than enough)*

### 1.2 `FB_PAGE_ID` + `FB_PAGE_ACCESS_TOKEN`
Follow `docs/FACEBOOK_SETUP.md` exactly. You'll get a **System User Access
Token** that never expires.

### 1.3 `YT_CLIENT_SECRET_B64` + `YT_TOKEN_B64`
YouTube needs OAuth — you have to authorize it ONCE from your PC (which
produces a `token.json`), then upload both files as base64-encoded secrets.

#### 1.3.a Create the OAuth client in Google Cloud
1. Go to https://console.cloud.google.com/.
2. Create a new project (any name, e.g. `auto-poster`).
3. Top search → **YouTube Data API v3** → **Enable**.
4. Left sidebar → **APIs & Services → OAuth consent screen**.
   - User type: **External**, click Create.
   - App name: anything (e.g. `My Auto Poster`).
   - Support email: your email.
   - Save → Save → Save through to the end.
   - **Test users** → add your own Google account email → Save.
5. Left sidebar → **Credentials → Create Credentials → OAuth Client ID**.
   - Application type: **Desktop app**, name it anything.
   - Click Create → **Download JSON**.
   - Rename the downloaded file to `client_secret.json`.
   - Move it to `C:\Users\golam\CascadeProjects\SocialAutoPoster\client_secret.json`.

#### 1.3.b Authorize once on your PC
```powershell
cd C:\Users\golam\CascadeProjects\SocialAutoPoster
.\.venv\Scripts\activate
python -c "from automation import youtube; youtube.authorize_interactive()"
```
A browser opens → choose your Google account → click **Continue** through any
"unverified app" warnings (it's your own app) → close the browser.

This creates `automation_data/token.json`.

#### 1.3.c Base64-encode both files
```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("client_secret.json")) | Set-Clipboard
# That copies it. Paste into Notepad and label it "YT_CLIENT_SECRET_B64".

[Convert]::ToBase64String([IO.File]::ReadAllBytes("automation_data\token.json")) | Set-Clipboard
# Same — paste and label "YT_TOKEN_B64".
```

---

## Step 2 — Put all 5 secrets into GitHub

1. Open https://github.com/golamdastagirnahid-blip/SocialAutoPoster
2. **Settings → Secrets and variables → Actions → New repository secret**
3. Add each one with these EXACT names:

| Secret name              | Value                                          |
|--------------------------|------------------------------------------------|
| `GROQ_API_KEY`           | from 1.1                                       |
| `FB_PAGE_ID`             | numeric Page ID                                |
| `FB_PAGE_ACCESS_TOKEN`   | long EAA… token                                |
| `YT_CLIENT_SECRET_B64`   | base64 string from 1.3.c (client_secret.json)  |
| `YT_TOKEN_B64`           | base64 string from 1.3.c (token.json)          |

Optional secret:
| `OPENROUTER_API_KEY`     | only if you want OpenRouter fallback for AI    |

---

## Step 3 — (optional) Tune behavior with Variables

Same screen, switch to the **Variables** tab. Add any of these to override
defaults (all optional):

| Variable name        | Default | What it does                              |
|----------------------|---------|-------------------------------------------|
| `YT_PRIVACY`         | public  | `public`, `unlisted`, or `private`        |
| `ACTIVE_HOUR_START`  | 9       | Earliest hour of day (UTC) to start posting |
| `ACTIVE_HOUR_END`    | 23      | Latest hour of day (UTC)                  |

---

## Step 4 — Add your first source

The easy way (no terminal):
1. On GitHub, open `sources/facebook.txt`.
2. Click the ✏️ pencil icon to edit.
3. Add a line like `https://bunkr.cr/a/eqbJ1JL5`.
4. **Commit changes**.
5. Repeat for `sources/youtube.txt`.

---

## Step 5 — First test: force a post

1. Open the **Actions** tab in your repo.
2. Pick **Auto Post** workflow on the left.
3. Click **Run workflow** (top right of the runs list).
   - Force: `true`
   - Platform: pick `facebook` for first test (YT quota is precious).
4. Wait ~2 minutes. Watch the logs.
5. Check your Facebook Page — there should be a new post.

If it failed, click the failed step → read the logs → fix the secret named
in the error → re-run.

---

## Step 6 — Let it run forever

Done! The hourly cron is already active. From now on:

- **Add content**: edit `sources/facebook.txt` or `sources/youtube.txt`,
  commit.
- **Watch post history**: open `automation_data/state.json` in the repo —
  the `history` array has every post with timestamp + remote ID.
- **Force a post**: Actions → Auto Post → Run workflow.
- **Pause a platform**: edit `automation_data/state.json`, set
  `"paused": true` for that platform, commit. (Or wait — it auto-pauses
  after 5 consecutive failures.)
- **Resume**: set `"paused": false`, commit.

---

## Cost & limits

- GitHub Actions on a public repo: **unlimited free minutes**.
- Our workflow uses about **30 seconds per hour** when idle, **3-5 minutes**
  when actually posting.
- YouTube Data API: 10,000 units/day → ~6 uploads/day max. We do 1.
- Facebook: no rate limit at 1 post/day.

---

## Troubleshooting

**"YT_TOKEN_B64 missing or invalid"**
Re-do Step 1.3 — the token may have expired (Google sometimes invalidates
unused tokens after 7 days in testing mode). Promote your OAuth consent
screen to **Production** in Google Cloud to make tokens permanent.

**"validation failed: ..." in logs**
The downloaded video is too big or wrong shape for the platform. The bot
already skipped it and will try the next file next tick.

**Facebook returns 190 / OAuth error**
Your token is broken or wrong permissions. Run `python -m automation.fb_check`
locally to diagnose.

**Bot is stuck — nothing posts**
Force-trigger from the Actions tab (Step 5). Watch the logs to see why.

---

## What's still local-only

You can also run `python run.py` on your PC to get the Flask dashboard for
inspection and manual queue management. It uses the same source files and
SQLite queue but is **not** required when the GitHub Actions runner is
active. Pick whichever you prefer.
