"""Entrypoint for the FB+YT auto-poster dashboard.

    python run.py

Then open http://127.0.0.1:5000
"""
from automation.app import create_app
from automation import config

if __name__ == "__main__":
    app = create_app()
    print(f"Dashboard: http://{config.HOST}:{config.PORT}")
    app.run(host=config.HOST, port=config.PORT, debug=False, use_reloader=False, threaded=True)
