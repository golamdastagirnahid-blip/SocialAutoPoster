"""Generate human-feeling title / description / hashtags via Groq (primary) + OpenRouter (fallback)."""
import json, re, random, requests
from . import config, db

SYSTEM = (
    "You are a viral social-media copywriter. Output STRICT JSON only, no markdown, no commentary."
    " Keys: title (<=90 chars, hook-style, no clickbait emojis spam, 0-2 emojis max), "
    "description (2-4 short lines, natural human tone, ends with a call to engage), "
    "tags (array of 12-20 lowercase hashtag strings WITHOUT the # symbol, no spaces, relevant + trending mix)."
)

def _user_prompt(platform, niche, language, filename_hint):
    target = "YouTube Shorts" if platform == "youtube" else "Facebook Reels/Post"
    return (
        f"Platform: {target}\nNiche: {niche}\nLanguage: {language}\n"
        f"Media filename hint (may be noisy, ignore if irrelevant): {filename_hint}\n"
        "Write the post. Remember: STRICT JSON only."
    )

def _extract_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m: raise ValueError("no json in response")
    return json.loads(m.group(0))

def _call_groq(prompt):
    if not config.GROQ_API_KEY: raise RuntimeError("no groq key")
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {config.GROQ_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": config.GROQ_MODEL,
            "temperature": 0.9,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": prompt}],
        }, timeout=60)
    r.raise_for_status()
    return _extract_json(r.json()["choices"][0]["message"]["content"])

def _call_openrouter(prompt):
    if not config.OPENROUTER_API_KEY: raise RuntimeError("no openrouter key")
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": config.OPENROUTER_MODEL,
            "temperature": 0.9,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": prompt}],
        }, timeout=90)
    r.raise_for_status()
    return _extract_json(r.json()["choices"][0]["message"]["content"])

def _fallback(filename_hint, platform):
    base = re.sub(r"[_\-]+", " ", (filename_hint or "amazing clip")).strip().title()[:60]
    suffix = "#Shorts" if platform == "youtube" else ""
    tags = ["shorts","viral","trending","fyp","reels","video","explore","like","share","follow","amazing","wow"]
    return {
        "title": f"{base} {suffix}".strip(),
        "description": f"{base}\n\nDrop a like if you enjoyed and follow for more!\n{config.DEFAULT_HASHTAGS}",
        "tags": tags,
    }

def generate(platform, filename_hint=""):
    prompt = _user_prompt(platform, config.NICHE, config.LANGUAGE, filename_hint)
    for fn, name in ((_call_groq, "groq"), (_call_openrouter, "openrouter")):
        try:
            data = fn(prompt)
            data["title"] = str(data.get("title", ""))[:95] or "New Post"
            data["description"] = str(data.get("description", ""))[:4500]
            tags = data.get("tags") or []
            if isinstance(tags, str): tags = re.split(r"[,\s]+", tags)
            tags = [re.sub(r"[^A-Za-z0-9_]", "", t).lower() for t in tags if t]
            data["tags"] = [t for t in tags if t][:25]
            db.log("INFO", f"ai({name}) ok: {data['title']}")
            return data
        except Exception as e:
            db.log("WARN", f"ai({name}) failed: {e}")
    db.log("WARN", "ai fallback (template)")
    return _fallback(filename_hint, platform)
