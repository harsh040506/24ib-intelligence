"""Entry point: `python run.py` boots the full platform (web + scheduler)."""
from __future__ import annotations

from intelligence import create_app

app = create_app()

if __name__ == "__main__":
    app.run(
        host=app.config["HOST"], port=app.config["PORT"],
        debug=(app.config["ENV"] == "development"),
        use_reloader=app.config["USE_RELOADER"],
    )
