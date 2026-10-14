import logging
import os
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv


def configure_app(app, overrides=None):
    load_dotenv()
    debug_mode = os.getenv("FLASK_DEBUG", "false").lower() == "true"

    app.secret_key = os.getenv("SECRET_KEY")
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=not debug_mode,
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )
    if overrides:
        app.config.update(overrides)

    if not app.debug:
        os.makedirs("logs", exist_ok=True)
        file_handler = RotatingFileHandler(
            "logs/app.log",
            maxBytes=1_000_000,
            backupCount=5,
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s: %(message)s")
        )
        file_handler.setLevel(logging.INFO)
        app.logger.addHandler(file_handler)
        app.logger.setLevel(logging.INFO)
