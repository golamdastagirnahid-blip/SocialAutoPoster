"""Verify the configured FB Page token has every permission we need to post.

Run with:  python -m automation.fb_check
"""
import sys, requests
from . import config

GRAPH = "https://graph.facebook.com/v20.0"
REQUIRED = {
    "pages_manage_posts",
    "pages_manage_engagement",
    "pages_read_engagement",
    "pages_show_list",
}
NICE_TO_HAVE = {"business_management", "read_insights"}

def _get(path, **params):
    params["access_token"] = config.FB_PAGE_TOKEN
    r = requests.get(f"{GRAPH}/{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def main():
    if not config.FB_PAGE_TOKEN or not config.FB_PAGE_ID:
        print("✗ FB_PAGE_ID and/or FB_PAGE_ACCESS_TOKEN missing in .env")
        sys.exit(1)
    ok = True

    # 1) Token introspection
    try:
        debug = requests.get(f"{GRAPH}/debug_token",
            params={"input_token": config.FB_PAGE_TOKEN,
                    "access_token": config.FB_PAGE_TOKEN},
            timeout=30).json().get("data", {})
    except Exception as e:
        print(f"✗ token debug failed: {e}"); sys.exit(1)

    if not debug.get("is_valid"):
        print(f"✗ token invalid: {debug.get('error', {}).get('message', '?')}")
        sys.exit(1)

    expires = debug.get("expires_at", 0)
    if expires == 0:
        print("✓ token never expires (System User token — perfect)")
    else:
        from datetime import datetime
        d = datetime.fromtimestamp(expires)
        print(f"⚠ token expires {d:%Y-%m-%d} (consider switching to System User token)")

    # 2) Scopes
    have = set(debug.get("scopes", []))
    missing = REQUIRED - have
    if missing:
        print(f"✗ missing required scopes: {sorted(missing)}")
        ok = False
    else:
        print(f"✓ all required scopes present ({len(have & REQUIRED)}/{len(REQUIRED)})")
    nice_missing = NICE_TO_HAVE - have
    if nice_missing:
        print(f"⚠ optional scopes missing (won't block posting): {sorted(nice_missing)}")

    # 3) Page identity
    try:
        page = _get(config.FB_PAGE_ID, fields="id,name,can_post,verification_status")
        print(f"✓ token sees Page: {page.get('name')} (id {page.get('id')})")
        if page.get("can_post") is False:
            print("✗ token cannot post to this Page — re-check System User permissions")
            ok = False
    except requests.HTTPError as e:
        print(f"✗ cannot read Page {config.FB_PAGE_ID}: {e.response.text[:200]}")
        ok = False

    # 4) Page <-> token match check (very common mistake: user-token instead of page-token)
    try:
        me = _get("me", fields="id,name")
        if str(me.get("id")) != str(config.FB_PAGE_ID):
            print(f"⚠ token's /me id ({me.get('id')}) ≠ FB_PAGE_ID ({config.FB_PAGE_ID})")
            print("  This is fine for a System User token assigned to multiple Pages,")
            print("  but if posting fails, double-check the System User has Full Control of the Page.")
        else:
            print(f"✓ token /me matches FB_PAGE_ID")
    except Exception as e:
        print(f"⚠ /me check skipped: {e}")

    print()
    print("✓ ready to post" if ok else "✗ fix the issues above before going live")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
