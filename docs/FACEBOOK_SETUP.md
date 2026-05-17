# Facebook System User Token Setup (recommended)

This is the **production-grade** way to authenticate. The token never expires,
which is exactly what you want for an auto-poster running 24/7.

> Requires: a Meta **Business Account** (free) + your Facebook **Page** added to it.

---

## Step 1 — Make sure your Page is in Business Manager

1. Go to https://business.facebook.com → **Business Settings**.
2. Left sidebar → **Accounts** → **Pages**.
3. If your Page isn't listed: click **Add** → **Add a Page** → enter the Page
   name/URL → confirm. (You must be an admin of the Page.)

---

## Step 2 — Create a System User

1. Business Settings → **Users** → **System Users** → **Add**.
2. Name it anything (e.g. `auto-poster-bot`).
3. Role: **Admin** (required for posting).
4. Click **Create System User**.

---

## Step 3 — Assign the Page to the System User

1. With the System User selected, click **Add Assets** → **Pages**.
2. Select your Page.
3. Toggle ON every **Full Control** permission (especially **Manage Page**,
   **Create Content**, **Manage Page Posts**).
4. Save.

---

## Step 4 — Generate the never-expiring token

1. Still on the System User row → **Generate New Token**.
2. Pick the **App** the token will belong to (any app you control;
   if you don't have one create a free **Business** type app at
   https://developers.facebook.com/apps).
3. Token expiration: **Never**.
4. **Required scopes** — check ALL of these:
   - `pages_manage_posts`
   - `pages_manage_engagement`
   - `pages_read_engagement`
   - `pages_show_list`
   - `business_management`
   - `read_insights`
5. **Generate token** → copy the long string (starts with `EAA...`).

---

## Step 5 — Find your Page ID

1. Open your Page on Facebook.
2. Go to **About** → scroll to **Page transparency** → **Page ID**.
3. Copy the numeric ID (e.g. `995488536983574`).

---

## Step 6 — Put both into `.env`

Edit `c:\Users\golam\CascadeProjects\SocialAutoPoster\.env`:

```
FB_PAGE_ID=995488536983574
FB_PAGE_ACCESS_TOKEN=EAA...your_long_token_here...
```

---

## Step 7 — Verify it works

```powershell
cd C:\Users\golam\CascadeProjects\SocialAutoPoster
.\.venv\Scripts\activate
python -m automation.fb_check
```

You should see:
```
✓ token belongs to Page: <Your Page Name> (id 995488536983574)
✓ token never expires
✓ scopes ok: pages_manage_posts, pages_manage_engagement, ...
✓ ready to post
```

If any check fails, the script tells you exactly which permission is missing
and where to fix it in Business Settings.

---

## Security tips

- **Never** commit `.env` (already in `.gitignore`).
- Treat the token like a password — anyone with it can post to your Page.
- If leaked: Business Settings → Users → System Users → your bot →
  **Refresh token** → all old tokens immediately invalidated.
