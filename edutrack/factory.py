from pathlib import Path

from flask import Flask

from .config import configure_app
from .extensions import csrf, limiter, login_manager
from .routes import register_routes


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def create_app(config=None):
    app = Flask(
        __name__,
        root_path=str(PROJECT_ROOT),
        template_folder="templates",
    )
    configure_app(app, config)

    csrf.init_app(app)
    limiter.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "login"
    login_manager.login_message = "Please log in to access this page."

    register_routes(app)
    return app
