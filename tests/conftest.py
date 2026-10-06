import pytest

import app as edutrack


@pytest.fixture
def client(monkeypatch):
    edutrack.app.config.update(
        TESTING=True,
        SECRET_KEY="test-only-secret-key-do-not-use-outside-tests",
        SESSION_COOKIE_SECURE=False,
        WTF_CSRF_ENABLED=True,
        RATELIMIT_ENABLED=False,
    )
    users = {}
    monkeypatch.setattr(
        edutrack.login_manager,
        "_user_callback",
        lambda user_id: users.get(user_id),
    )
    return edutrack.app.test_client(), users
