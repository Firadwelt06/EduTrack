from flask_login import LoginManager
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect


csrf = CSRFProtect()
limiter = Limiter(
    get_remote_address,
    storage_uri="memory://",
)
login_manager = LoginManager()
