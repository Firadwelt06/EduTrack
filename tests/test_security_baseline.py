import io
import re

import app as edutrack


class FakeCursor:
    def __init__(self):
        self.queries = []
        self.closed = False

    def execute(self, query, params=None):
        self.queries.append((query, params))

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self):
        self.fake_cursor = FakeCursor()
        self.closed = False

    def cursor(self, **kwargs):
        return self.fake_cursor

    def close(self):
        self.closed = True


def authenticate(client, users, role, user_id="1", student_id=None, teacher_id=None):
    users[user_id] = edutrack.User(
        int(user_id), "test-user", role,
        student_id=student_id, teacher_id=teacher_id,
    )
    with client.session_transaction() as session:
        session["_user_id"] = user_id
        session["_fresh"] = True


def csrf_token(client):
    response = client.get("/login")
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert match is not None
    return match.group(1).decode()


def test_private_route_redirects_anonymous_user(client):
    test_client, _ = client

    response = test_client.get("/settings")

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login")


def test_student_cannot_open_admin_settings_without_database_access(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "student", student_id=7)
    monkeypatch.setattr(
        edutrack, "get_conn",
        lambda: (_ for _ in ()).throw(AssertionError("database must not be accessed")),
    )

    response = test_client.get("/settings")

    assert response.status_code == 302
    assert response.headers["Location"] == "/"


def test_teacher_cannot_read_unassigned_student_and_connection_is_closed(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "teacher", teacher_id=4)
    connection = FakeConnection()
    monkeypatch.setattr(edutrack, "get_conn", lambda: connection)

    response = test_client.get("/students/9")

    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    assert len(connection.fake_cursor.queries) == 1
    assert connection.fake_cursor.closed
    assert connection.closed


def test_teacher_cannot_submit_student_enrollment(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "teacher", teacher_id=4)
    connection = FakeConnection()
    monkeypatch.setattr(edutrack, "get_conn", lambda: connection)

    response = test_client.post(
        "/grades?year_id=1&semester_id=2",
        data={
            "csrf_token": csrf_token(test_client),
            "action": "enroll",
            "student_id": "7",
            "course_id": "5",
        },
    )

    assert response.status_code == 200
    assert b"Only admins can enroll students." in response.data
    assert not any("INSERT INTO enrollments" in query for query, _ in connection.fake_cursor.queries)
    assert connection.fake_cursor.closed
    assert connection.closed


def test_invalid_grade_is_rejected_before_database_update(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "admin")
    connection = FakeConnection()
    monkeypatch.setattr(edutrack, "get_conn", lambda: connection)

    response = test_client.post(
        "/grades?year_id=1&semester_id=2",
        data={
            "csrf_token": csrf_token(test_client),
            "action": "update_grade",
            "enrollment_id": "3",
            "new_grade": "A+",
        },
    )

    assert response.status_code == 200
    assert b"Please select a valid grade." in response.data
    assert not any("UPDATE enrollments" in query for query, _ in connection.fake_cursor.queries)
    assert connection.fake_cursor.closed
    assert connection.closed


def test_student_import_rejects_invalid_headers_without_database_access(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "admin")
    monkeypatch.setattr(
        edutrack, "get_conn",
        lambda: (_ for _ in ()).throw(AssertionError("invalid CSV must not access the database")),
    )

    response = test_client.post(
        "/students/import",
        data={
            "csrf_token": csrf_token(test_client),
            "csv_file": (io.BytesIO(b"unexpected,headers\none,two\n"), "students.csv"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert b"valid student CSV" in response.data


def test_student_import_rejects_non_utf8_csv_without_database_access(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "admin")
    monkeypatch.setattr(
        edutrack, "get_conn",
        lambda: (_ for _ in ()).throw(AssertionError("invalid CSV must not access the database")),
    )

    response = test_client.post(
        "/students/import",
        data={
            "csrf_token": csrf_token(test_client),
            "csv_file": (io.BytesIO(b"\xff\xfe\xfa"), "students.csv"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert b"valid student CSV" in response.data


def test_missing_csrf_token_rejects_login_before_database_access(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr(
        edutrack, "get_conn",
        lambda: (_ for _ in ()).throw(AssertionError("CSRF rejection must happen first")),
    )

    response = test_client.post(
        "/login",
        data={"username": "admin", "password": "a-long-test-password"},
    )

    assert response.status_code == 302


def test_csv_formula_prefixes_are_neutralized():
    for trigger in ("=", "+", "-", "@", "\t", "\r"):
        assert edutrack.sanitize_csv_field(f"{trigger}1+1") == f"'{trigger}1+1"
    assert edutrack.sanitize_csv_field("ordinary text") == "ordinary text"
    assert edutrack.sanitize_csv_field("") == ""


def test_login_backoff_starts_after_two_failures_and_caps_at_five_minutes():
    assert edutrack.backoff_seconds(1) == 0
    assert edutrack.backoff_seconds(2) == 0
    assert edutrack.backoff_seconds(3) == 2
    assert edutrack.backoff_seconds(4) == 4
    assert edutrack.backoff_seconds(20) == 300
