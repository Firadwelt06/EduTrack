from edutrack.factory import create_app


def test_application_factory_registers_routes_and_applies_overrides():
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "factory-test-secret",
            "SESSION_COOKIE_SECURE": False,
        }
    )

    endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}
    assert "login" in endpoints
    assert "student_detail" in endpoints
    assert app.config["SECRET_KEY"] == "factory-test-secret"
    assert app.config["TESTING"] is True
    assert app.config["SESSION_COOKIE_SECURE"] is False
