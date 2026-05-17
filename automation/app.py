"""Flask dashboard."""
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from . import config, db, worker, youtube as yt

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = config.SECRET

@app.template_filter("ts")
def _ts(v):
    if not v: return "-"
    try: return datetime.fromtimestamp(int(v)).strftime("%Y-%m-%d %H:%M")
    except: return str(v)

@app.route("/")
def index():
    return render_template("index.html",
        fb_jobs=db.get_jobs(platform="facebook"),
        yt_jobs=db.get_jobs(platform="youtube"),
        stats=db.stats(),
        fb_state=db.get_platform_state("facebook"),
        yt_state=db.get_platform_state("youtube"),
        cfg=config)

@app.post("/platform/<platform>/<action>")
def platform_action(platform, action):
    if platform not in ("facebook", "youtube"):
        return "bad platform", 400
    if action == "pause":
        db.set_paused(platform, True, "manually paused")
        flash(f"{platform} paused", "ok")
    elif action == "resume":
        db.set_paused(platform, False)
        with db.conn() as c:
            c.execute("UPDATE platform_state SET consecutive_failures=0, last_error=NULL WHERE platform=?", (platform,))
        flash(f"{platform} resumed", "ok")
    return redirect(url_for("index"))

@app.post("/add/<platform>")
def add(platform):
    if platform not in ("facebook", "youtube"):
        return "bad platform", 400
    raw = request.form.get("urls", "")
    urls = [l.strip() for l in raw.splitlines() if l.strip()]
    n = db.add_jobs(platform, urls)
    flash(f"Added {n} new {platform} source(s).", "ok")
    return redirect(url_for("index"))

@app.post("/job/<int:job_id>/<action>")
def job_action(job_id, action):
    if action == "retry": db.retry_job(job_id)
    elif action == "delete": db.delete_job(job_id)
    return redirect(url_for("index"))

@app.get("/logs")
def logs():
    return render_template("logs.html", logs=db.recent_logs(500))

@app.get("/api/stats")
def api_stats():
    return jsonify({"stats": db.stats(), "fb": db.get_jobs(platform="facebook", limit=50),
                    "yt": db.get_jobs(platform="youtube", limit=50)})

@app.post("/yt/authorize")
def yt_authorize():
    try:
        yt.authorize_interactive()
        flash("YouTube authorized.", "ok")
    except Exception as e:
        flash(f"YT auth failed: {e}", "err")
    return redirect(url_for("index"))

def create_app():
    db.init()
    worker.start()
    return app
