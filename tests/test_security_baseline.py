import io
import hashlib
from pathlib import Path
from urllib.error import URLError

from flask import session
from flask_wtf.csrf import generate_csrf

import app as edutrack


class FakeCursor:
    def __init__(self, one_results=(), all_results=()):
        self.queries = []
        self.one_results = list(one_results)
        self.all_results = list(all_results)
        self.lastrowid = 42
        self.closed = False

    def execute(self, query, params=None):
        self.queries.append((query, params))

    def fetchone(self):
        if self.one_results:
            return self.one_results.pop(0)
        return None

    def fetchall(self):
        if self.all_results:
            return self.all_results.pop(0)
        return []

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, one_results=(), all_results=()):
        self.fake_cursor = FakeCursor(one_results, all_results)
        self.closed = False
        self.commits = 0

    def cursor(self, **kwargs):
        return self.fake_cursor

    def close(self):
        self.closed = True

    def commit(self):
        self.commits += 1


class FailingAuditCursor(FakeCursor):
    def execute(self, query, params=None):
        super().execute(query, params)
        if "INSERT INTO audit_log" in query:
            raise edutrack.mysql.connector.DatabaseError("simulated audit write failure")


def authenticate(client, users, role, user_id="1", student_id=None, teacher_id=None):
    users[user_id] = edutrack.User(
        int(user_id), "test-user", role,
        student_id=student_id, teacher_id=teacher_id,
    )
    with client.session_transaction() as session:
        session["_user_id"] = user_id
        session["_fresh"] = True


def csrf_token(client):
    with edutrack.app.test_request_context():
        token = generate_csrf()
        session_token = session["csrf_token"]
    with client.session_transaction() as client_session:
        client_session["csrf_token"] = session_token
    return token


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


def test_student_cannot_read_another_students_record(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "student", student_id=7)
    connection = FakeConnection()
    monkeypatch.setattr(edutrack, "get_conn", lambda: connection)

    response = test_client.get("/students/9")

    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    assert connection.fake_cursor.queries == []
    assert connection.fake_cursor.closed
    assert connection.closed


def test_unknown_role_cannot_read_student_or_course_details(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "unknown-role")
    connections = []

    def fake_get_conn():
        connection = FakeConnection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(edutrack, "get_conn", fake_get_conn)

    student_response = test_client.get("/students/9")
    course_response = test_client.get("/courses/5")

    assert student_response.status_code == 403
    assert course_response.status_code == 403
    assert len(connections) == 2
    for connection in connections:
        assert connection.fake_cursor.queries == []
        assert connection.fake_cursor.closed
        assert connection.closed


def test_teacher_cannot_open_a_course_owned_by_another_teacher(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "teacher", teacher_id=4)
    connection = FakeConnection()
    monkeypatch.setattr(edutrack, "get_conn", lambda: connection)

    response = test_client.get("/courses/5")

    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    assert len(connection.fake_cursor.queries) == 1
    assert connection.fake_cursor.closed
    assert connection.closed


def test_student_course_detail_never_queries_classmates_or_their_grades(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "student", student_id=7)
    connection = FakeConnection(
        one_results=[
            {"allowed": 1},
            {"course_id": 5, "course_name": "Math", "capacity": 20,
             "teacher": "Teacher", "teacher_email": "teacher@example.edu"},
        ],
        all_results=[
            [{"year_id": 1, "year_name": "2026-2027"}],
            [{"semester_id": 2, "semester_name": "Fall"}],
            [],
            [],
        ],
    )
    monkeypatch.setattr(edutrack, "get_conn", lambda: connection)

    response = test_client.get("/courses/5?year_id=1&semester_id=2")

    assert response.status_code == 200
    student_queries = [
        (query, params)
        for query, params in connection.fake_cursor.queries
        if "FROM enrollments e" in query and "student_id = %s" in query
    ]
    assert len(student_queries) == 2
    assert all(params == [5, 2, 7] for _, params in student_queries)
    assert connection.fake_cursor.closed
    assert connection.closed


def test_teacher_dashboard_queries_are_scoped_to_owned_courses(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "teacher", teacher_id=4)
    connection = FakeConnection(one_results=[{"count": 2}, {"count": 1}, {"count": 3}])
    monkeypatch.setattr(edutrack, "get_conn", lambda: connection)

    response = test_client.get("/?year_id=1&semester_id=2")

    assert response.status_code == 200
    enrollment_queries = [
        (query, params)
        for query, params in connection.fake_cursor.queries
        if "FROM enrollments e" in query
    ]
    assert len(enrollment_queries) == 5
    for query, params in enrollment_queries:
        assert "teacher_id = %s" in query
        assert params == [2, 4]
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


def test_setting_missing_academic_year_does_not_clear_current_year(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "admin")
    connection = FakeConnection(one_results=[None])
    monkeypatch.setattr(edutrack, "get_conn", lambda: connection)

    response = test_client.post(
        "/settings?tab=academic",
        data={
            "csrf_token": csrf_token(test_client),
            "action": "set_current_year",
            "year_id": "999",
        },
    )

    assert response.status_code == 200
    assert b"That academic year does not exist." in response.data
    assert not any(
        "UPDATE academic_years SET is_current" in query
        for query, _ in connection.fake_cursor.queries
    )
    assert connection.commits == 0
    assert connection.fake_cursor.closed
    assert connection.closed


def test_setting_existing_academic_year_commits_both_updates_together(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "admin")
    connection = FakeConnection(one_results=[{"year_id": 2}])
    monkeypatch.setattr(edutrack, "get_conn", lambda: connection)

    response = test_client.post(
        "/settings?tab=academic",
        data={
            "csrf_token": csrf_token(test_client),
            "action": "set_current_year",
            "year_id": "2",
        },
    )

    assert response.status_code == 200
    assert b"Current academic year updated." in response.data
    updates = [
        (query, params)
        for query, params in connection.fake_cursor.queries
        if "UPDATE academic_years SET is_current" in query
    ]
    assert len(updates) == 2
    assert updates[0][1] is None
    assert updates[1][1] == ("2",)
    assert connection.commits == 1
    assert connection.fake_cursor.closed
    assert connection.closed


def test_teacher_cannot_update_grade_for_another_teachers_course(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "teacher", teacher_id=4)
    connection = FakeConnection(one_results=[None])
    monkeypatch.setattr(edutrack, "get_conn", lambda: connection)

    response = test_client.post(
        "/grades?year_id=1&semester_id=2",
        data={
            "csrf_token": csrf_token(test_client),
            "action": "update_grade",
            "enrollment_id": "3",
            "new_grade": "A",
        },
    )

    assert response.status_code == 200
    assert b"update grades for a course" in response.data
    assert not any("UPDATE enrollments" in query for query, _ in connection.fake_cursor.queries)
    assert connection.commits == 0
    assert connection.fake_cursor.closed
    assert connection.closed


def test_teacher_can_update_grade_for_owned_course(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "teacher", teacher_id=4)
    connection = FakeConnection(
        one_results=[{"allowed": 1}, {"final_grade": "B"}]
    )
    monkeypatch.setattr(edutrack, "get_conn", lambda: connection)

    response = test_client.post(
        "/grades?year_id=1&semester_id=2",
        data={
            "csrf_token": csrf_token(test_client),
            "action": "update_grade",
            "enrollment_id": "3",
            "new_grade": "A",
        },
    )

    assert response.status_code == 200
    assert b"Grade updated to A successfully." in response.data
    grade_update = next(
        (query, params)
        for query, params in connection.fake_cursor.queries
        if "UPDATE enrollments SET final_grade" in query
    )
    assert grade_update[1] == ("A", "3")
    assert connection.commits == 1
    assert any("INSERT INTO audit_log" in query for query, _ in connection.fake_cursor.queries)
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


def test_student_import_inserts_a_valid_row(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "admin")
    connection = FakeConnection(one_results=[None])
    monkeypatch.setattr(edutrack, "get_conn", lambda: connection)

    response = test_client.post(
        "/students/import",
        data={
            "csrf_token": csrf_token(test_client),
            "csv_file": (
                io.BytesIO(b"first_name,last_name,email,grade_level\nJane,Doe,jane@example.edu,10\n"),
                "students.csv",
            ),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert b"Added 1, updated 0, skipped 0." in response.data
    student_insert = next(
        (query, params)
        for query, params in connection.fake_cursor.queries
        if "INSERT INTO students" in query
    )
    assert student_insert[1] == ("Jane", None, "Doe", None, "jane@example.edu", None, None, None, "10")
    assert connection.commits == 1
    audit_params = next(
        params
        for query, params in connection.fake_cursor.queries
        if "INSERT INTO audit_log" in query
    )
    assert audit_params[2:5] == ("create", "student", 42)
    assert "jane@example.edu" not in audit_params[5]
    assert connection.fake_cursor.closed
    assert connection.closed


def test_student_import_rolls_back_a_row_when_its_audit_write_fails(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "admin")
    connection = FakeConnection(one_results=[None])
    connection.fake_cursor = FailingAuditCursor(one_results=[None])
    monkeypatch.setattr(edutrack, "get_conn", lambda: connection)
    monkeypatch.setattr(edutrack, "log_db_error", lambda exc: "test-ref")

    response = test_client.post(
        "/students/import",
        data={
            "csrf_token": csrf_token(test_client),
            "csv_file": (
                io.BytesIO(b"first_name,last_name,email,grade_level\nJane,Doe,jane@example.edu,10\n"),
                "students.csv",
            ),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert b"Added 0, updated 0, skipped 1." in response.data
    statements = [query for query, _ in connection.fake_cursor.queries]
    assert any("INSERT INTO students" in query for query in statements)
    assert any(query.startswith("ROLLBACK TO SAVEPOINT student_import_2") for query in statements)
    assert any(query.startswith("RELEASE SAVEPOINT student_import_2") for query in statements)
    assert connection.commits == 1
    assert connection.fake_cursor.closed
    assert connection.closed


def test_student_edit_writes_audit_event_without_personal_data(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "admin")
    connection = FakeConnection()
    monkeypatch.setattr(edutrack, "get_conn", lambda: connection)

    response = test_client.post(
        "/students/9/edit",
        data={
            "csrf_token": csrf_token(test_client),
            "fname": "Jane",
            "mname": "",
            "lname": "Doe",
            "date_of_birth": "",
            "email": "jane@example.edu",
            "address": "",
            "guardian_name": "",
            "guardian_phone": "",
            "grade_level": "10",
        },
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/students/9"
    audit_params = next(
        params
        for query, params in connection.fake_cursor.queries
        if "INSERT INTO audit_log" in query
    )
    assert audit_params[2:5] == ("update", "student", 9)
    assert "jane@example.edu" not in audit_params[5]
    assert connection.commits == 1
    assert connection.fake_cursor.closed
    assert connection.closed


def test_student_creation_writes_audit_event_without_personal_data(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "admin")
    connection = FakeConnection()
    monkeypatch.setattr(edutrack, "get_conn", lambda: connection)

    response = test_client.post(
        "/add-student",
        data={
            "csrf_token": csrf_token(test_client),
            "fname": "Jane",
            "mname": "",
            "lname": "Doe",
            "date_of_birth": "",
            "email": "jane@example.edu",
            "address": "",
            "guardian_name": "",
            "guardian_phone": "",
            "grade_level": "10",
        },
    )

    assert response.status_code == 200
    audit_query, audit_params = next(
        (query, params)
        for query, params in connection.fake_cursor.queries
        if "INSERT INTO audit_log" in query
    )
    assert "INSERT INTO audit_log" in audit_query
    assert audit_params[2:5] == ("create", "student", 42)
    assert audit_params[5] == (
        '{"fields": ["first_name", "middle_name", "last_name", "date_of_birth", '
        '"email", "address", "guardian_name", "guardian_phone", "grade_level"]}'
    )
    assert "jane@example.edu" not in audit_params[5]
    assert connection.commits == 1
    assert connection.fake_cursor.closed
    assert connection.closed


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


def test_breached_password_check_uses_only_hash_prefix(monkeypatch):
    password = "a-long-test-password"
    password_hash = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
    requested_urls = []

    def fake_urlopen(request, timeout):
        requested_urls.append(request.full_url)
        return io.BytesIO(f"{password_hash[5:]}:42\n".encode("ascii"))

    monkeypatch.setattr(edutrack.urllib.request, "urlopen", fake_urlopen)

    result = edutrack.validate_new_password(password)

    assert result == (
        "This password has appeared in a known data breach. "
        "Please choose a different one."
    )
    assert requested_urls == [
        f"https://api.pwnedpasswords.com/range/{password_hash[:5]}"
    ]


def test_password_check_skips_external_request_for_too_short_password(monkeypatch):
    monkeypatch.setattr(
        edutrack.urllib.request, "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("short passwords must be rejected before the API call")
        ),
    )

    assert edutrack.validate_new_password("short") == "Password must be at least 12 characters."


def test_password_check_allows_change_when_breach_service_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        edutrack.urllib.request, "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(URLError("offline")),
    )

    assert edutrack.validate_new_password("a-long-test-password") is None


def test_install_schema_defines_tables_without_seeded_records():
    schema_path = Path(__file__).parents[1] / "database" / "schema.sql"
    schema = schema_path.read_text(encoding="utf-8")

    assert "CREATE TABLE `students`" in schema
    assert "CREATE TABLE `users`" in schema
    assert "CREATE TABLE `audit_log`" in schema
    assert "INSERT INTO" not in schema.upper()
