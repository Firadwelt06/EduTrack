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
        edutrack.routes, "get_conn",
        lambda: (_ for _ in ()).throw(AssertionError("database must not be accessed")),
    )

    response = test_client.get("/settings")

    assert response.status_code == 302
    assert response.headers["Location"] == "/"


def test_teacher_cannot_read_unassigned_student_and_connection_is_closed(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "teacher", teacher_id=4)
    connection = FakeConnection()
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

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
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

    response = test_client.get("/students/9")

    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    assert connection.fake_cursor.queries == []
    assert connection.fake_cursor.closed
    assert connection.closed


def test_student_course_catalog_only_includes_courses_for_selected_semester():
    query, params = edutrack.routes._course_listing_query(
        "student",
        student_id=17,
        semester_id=4,
        search="Biology",
    )

    assert "access_enrollment.student_id = %s" in query
    assert "access_enrollment.semester_id = %s" in query
    assert "c.course_name LIKE %s" in query
    assert params == [4, 17, 4, "%Biology%", "%Biology%"]


def test_student_course_catalog_is_empty_without_a_selected_semester():
    query, params = edutrack.routes._course_listing_query(
        "student",
        student_id=17,
        semester_id=None,
        search="",
    )

    assert "1 = 0" in query
    assert params == [None]


def test_student_course_export_keeps_enrollment_scope(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "student", student_id=17)
    connection = FakeConnection(
        all_results=[
            [
                {
                    "course_name": "Biology",
                    "teacher": "Alex Teacher",
                    "capacity": 30,
                    "enrolled": 18,
                }
            ]
        ]
    )
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

    response = test_client.get(
        "/courses/export?semester_id=4&search=Biology"
    )

    query, params = connection.fake_cursor.queries[0]
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert b"Biology,Alex Teacher" in response.data
    assert b"30" not in response.data
    assert "access_enrollment.student_id = %s" in query
    assert params == [4, 17, 4, "%Biology%", "%Biology%"]
    assert connection.fake_cursor.closed
    assert connection.closed


def test_course_catalog_search_renders_accessible_controls_and_responsive_navigation(
    client, monkeypatch
):
    test_client, users = client
    authenticate(test_client, users, "admin")
    connection = FakeConnection(
        all_results=[
            [{"year_id": 1, "year_name": "2026-2027"}],
            [{"semester_id": 4, "semester_name": "Fall"}],
            [
                {
                    "course_id": 2,
                    "course_name": "Biology",
                    "capacity": 20,
                    "teacher": "Alex Teacher",
                    "enrolled": 8,
                }
            ],
        ]
    )
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

    response = test_client.get(
        "/courses?year_id=1&semester_id=4&search=Biology"
    )

    assert response.status_code == 200
    assert b'Skip to main content' in response.data
    assert b'<main id="main-content"' in response.data
    assert b'aria-label="Main navigation"' in response.data
    assert b'@media (max-width: 760px)' in response.data
    assert b'<label for="course-search">Search courses</label>' in response.data
    assert "c.course_name LIKE %s" in connection.fake_cursor.queries[-1][0]
    assert connection.fake_cursor.queries[-1][1] == [4, "%Biology%", "%Biology%"]


def test_admin_can_remove_enrollment_with_audit_and_transaction(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "admin")
    connection = FakeConnection(
        one_results=[
            {
                "student_id": 17,
                "course_id": 4,
                "semester_id": 3,
                "academic_year_id": 2,
                "final_grade": "B",
            }
        ]
    )
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

    response = test_client.post(
        "/grades/enrollment/9/delete",
        data={"csrf_token": csrf_token(test_client)},
    )

    queries = connection.fake_cursor.queries
    assert response.status_code == 302
    assert response.headers["Location"] == "/grades?year_id=2&semester_id=3"
    assert any("DELETE FROM enrollments" in query for query, _ in queries)
    assert any("INSERT INTO audit_log" in query for query, _ in queries)
    assert connection.commits == 1
    assert connection.fake_cursor.closed
    assert connection.closed


def test_admin_can_update_teacher_and_audit_change(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "admin")
    connection = FakeConnection(
        one_results=[
            {
                "first_name": "Alex",
                "last_name": "Teacher",
                "email": "alex@example.edu",
                "hire_date": "2020-01-01",
            }
        ],
        all_results=[[], [], [], []],
    )
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

    response = test_client.post(
        "/settings?tab=teachers",
        data={
            "csrf_token": csrf_token(test_client),
            "action": "update_teacher",
            "teacher_id": "4",
            "fname": "Alex",
            "lname": "Educator",
            "email": "alex@example.edu",
            "hire_date": "2020-01-01",
        },
    )

    queries = connection.fake_cursor.queries
    assert response.status_code == 200
    assert b"Teacher details updated." in response.data
    assert any("UPDATE teachers" in query for query, _ in queries)
    assert any("INSERT INTO audit_log" in query for query, _ in queries)
    assert connection.commits == 1


def test_admin_can_update_course_and_audit_change(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "admin")
    connection = FakeConnection(
        one_results=[
            {
                "course_name": "Biology",
                "description": None,
                "capacity": 30,
                "teacher_id": 4,
            },
            {"count": 3},
        ],
        all_results=[[], [], [], []],
    )
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

    response = test_client.post(
        "/settings?tab=courses",
        data={
            "csrf_token": csrf_token(test_client),
            "action": "update_course",
            "course_id": "2",
            "course_name": "Advanced Biology",
            "description": "Upper-level biology",
            "capacity": "25",
            "teacher_id": "4",
        },
    )

    queries = connection.fake_cursor.queries
    assert response.status_code == 200
    assert b"Course details updated." in response.data
    assert any("UPDATE courses" in query for query, _ in queries)
    assert any("INSERT INTO audit_log" in query for query, _ in queries)
    assert connection.commits == 1


def test_admin_course_update_rejects_capacity_outside_supported_range(
    client, monkeypatch
):
    test_client, users = client
    authenticate(test_client, users, "admin")
    connection = FakeConnection(all_results=[[], [], [], []])
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

    response = test_client.post(
        "/settings?tab=courses",
        data={
            "csrf_token": csrf_token(test_client),
            "action": "update_course",
            "course_id": "2",
            "course_name": "Biology",
            "description": "",
            "capacity": "0",
            "teacher_id": "4",
        },
    )

    assert response.status_code == 200
    assert b"capacity from 1 to 200" in response.data
    assert not any("UPDATE courses" in query for query, _ in connection.fake_cursor.queries)
    assert connection.commits == 0


def test_admin_course_update_cannot_set_capacity_below_existing_enrollments(
    client, monkeypatch
):
    test_client, users = client
    authenticate(test_client, users, "admin")
    connection = FakeConnection(
        one_results=[
            {
                "course_name": "Biology",
                "description": None,
                "capacity": 30,
                "teacher_id": 4,
            },
            {"count": 12},
        ],
        all_results=[[], [], [], []],
    )
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

    response = test_client.post(
        "/settings?tab=courses",
        data={
            "csrf_token": csrf_token(test_client),
            "action": "update_course",
            "course_id": "2",
            "course_name": "Biology",
            "description": "",
            "capacity": "10",
            "teacher_id": "4",
        },
    )

    assert b"Capacity cannot be lower than the 12 existing enrollment(s)." in response.data
    assert not any("UPDATE courses" in query for query, _ in connection.fake_cursor.queries)
    assert connection.commits == 0


def test_teacher_student_export_scopes_students_and_neutralizes_csv_formulas(
    client, monkeypatch
):
    test_client, users = client
    authenticate(test_client, users, "teacher", teacher_id=9)
    connection = FakeConnection(
        all_results=[
            [
                {
                    "full_name": "=SUM(1,1)",
                    "email": "student@example.com",
                    "grade_level": 10,
                    "course_count": 2,
                }
            ]
        ]
    )
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

    response = test_client.get("/students/export?grade_level=10&search=Sam")

    query, params = connection.fake_cursor.queries[0]
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert b"'=SUM(1,1)" in response.data
    assert "WHERE c2.teacher_id = %s" in query
    assert params == [None, 9, 10, "%Sam%", "%Sam%"]
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

    monkeypatch.setattr(edutrack.routes, "get_conn", fake_get_conn)

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
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

    response = test_client.get("/courses/5")

    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    assert len(connection.fake_cursor.queries) == 1
    assert connection.fake_cursor.closed
    assert connection.closed


def test_admin_can_open_course_detail(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "admin")
    connection = FakeConnection(
        one_results=[
            {
                "course_id": 5,
                "course_name": "Biology",
                "capacity": 20,
                "teacher": "Alex Teacher",
                "teacher_email": "alex@example.edu",
            }
        ],
        all_results=[
            [{"year_id": 1, "year_name": "2026-2027"}],
            [{"semester_id": 2, "semester_name": "Fall"}],
            [],
            [],
        ],
    )
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

    response = test_client.get("/courses/5?year_id=1&semester_id=2")

    assert response.status_code == 200
    assert b"Biology" in response.data
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
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

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
    connection = FakeConnection(
        one_results=[{"count": 2}, {"count": 1}, {"count": 3}],
        all_results=[[], [{"semester_id": 2, "semester_name": "Fall"}]],
    )
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

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


def test_semester_selection_must_belong_to_selected_year():
    semesters = [
        {"semester_id": 3, "semester_name": "Spring"},
        {"semester_id": 4, "semester_name": "Fall"},
    ]

    assert edutrack.routes._semester_for_year(semesters, 4) == 4
    assert edutrack.routes._semester_for_year(semesters, 2) == 3
    assert edutrack.routes._semester_for_year([], 2) is None


def test_teacher_cannot_submit_student_enrollment(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "teacher", teacher_id=4)
    connection = FakeConnection()
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

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


def test_admin_enrollment_checks_capacity_and_year_semester_match(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "admin")
    connection = FakeConnection(
        one_results=[{"year_id": 8}],
        all_results=[
            [{"year_id": 1, "year_name": "2026-2027"}],
            [{"semester_id": 2, "semester_name": "Fall"}],
            [],
            [],
            [],
        ],
    )
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

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
    assert b"Choose a semester in the selected academic year." in response.data
    assert not any("FOR UPDATE" in query for query, _ in connection.fake_cursor.queries)
    assert not any("INSERT INTO enrollments" in query for query, _ in connection.fake_cursor.queries)


def test_admin_enrollment_rejects_course_at_capacity(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "admin")
    connection = FakeConnection(
        one_results=[
            {"year_id": 1},
            {"capacity": 2},
        ],
        all_results=[
            [{"year_id": 1, "year_name": "2026-2027"}],
            [{"semester_id": 2, "semester_name": "Fall"}],
            [{"enrollment_id": 1}, {"enrollment_id": 2}],
            [],
            [],
            [],
        ],
    )
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

    response = test_client.post(
        "/grades?year_id=1&semester_id=2",
        data={
            "csrf_token": csrf_token(test_client),
            "action": "enroll",
            "student_id": "7",
            "course_id": "5",
        },
    )

    assert b"This course is at capacity" in response.data
    assert any("FOR UPDATE" in query for query, _ in connection.fake_cursor.queries)
    assert not any("INSERT INTO enrollments" in query for query, _ in connection.fake_cursor.queries)
    assert connection.commits == 0


def test_admin_enrollment_commits_the_enrollment_and_audit_together(
    client, monkeypatch
):
    test_client, users = client
    authenticate(test_client, users, "admin")
    connection = FakeConnection(
        one_results=[
            {"year_id": 1},
            {"capacity": 30},
        ],
        all_results=[
            [{"year_id": 1, "year_name": "2026-2027"}],
            [{"semester_id": 2, "semester_name": "Fall"}],
            [],
            [],
            [],
            [],
        ],
    )
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

    response = test_client.post(
        "/grades?year_id=1&semester_id=2",
        data={
            "csrf_token": csrf_token(test_client),
            "action": "enroll",
            "student_id": "7",
            "course_id": "5",
        },
    )

    queries = connection.fake_cursor.queries
    assert b"Student enrolled successfully." in response.data
    assert any("INSERT INTO enrollments" in query for query, _ in queries)
    assert any("INSERT INTO audit_log" in query for query, _ in queries)
    assert connection.commits == 1
    assert connection.fake_cursor.closed
    assert connection.closed


def test_invalid_grade_is_rejected_before_database_update(client, monkeypatch):
    test_client, users = client
    authenticate(test_client, users, "admin")
    connection = FakeConnection()
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

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
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

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
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

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
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

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
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

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
        edutrack.routes, "get_conn",
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
        edutrack.routes, "get_conn",
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
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

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
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)
    monkeypatch.setattr(edutrack.routes, "log_db_error", lambda exc: "test-ref")

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
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

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
    monkeypatch.setattr(edutrack.routes, "get_conn", lambda: connection)

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
        edutrack.routes, "get_conn",
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

    assert "CREATE TABLE IF NOT EXISTS `students`" in schema
    assert "CREATE TABLE IF NOT EXISTS `users`" in schema
    assert "CREATE TABLE IF NOT EXISTS `audit_log`" in schema
    assert "INSERT INTO" not in schema.upper()


def test_daily_backup_uses_restricted_option_file_and_removes_it(client, monkeypatch, tmp_path):
    backup_folder = tmp_path / "backups"
    backup_folder.mkdir()
    option_file = backup_folder / "mysqldump_opts_test.cnf"
    option_file.write_text("[client]\npassword=test-only\n", encoding="utf-8")
    commands = []

    class FakeProcess:
        stdout = io.BytesIO(b"CREATE TABLE example (id INT);")
        returncode = 0

        def __init__(self, command, stdout):
            commands.append(command)

        def wait(self):
            return self.returncode

    monkeypatch.setattr(edutrack.backup, "BACKUP_FOLDER", str(backup_folder))
    monkeypatch.setattr(edutrack.backup, "create_secure_option_file", lambda: str(option_file))
    monkeypatch.setattr(edutrack.backup.keyring, "get_password", lambda *args: "backup-test-passphrase")
    monkeypatch.setattr(edutrack.subprocess, "Popen", FakeProcess)
    monkeypatch.setenv("DB_HOST", "localhost")
    monkeypatch.setenv("DB_USERNAME", "school_app")
    monkeypatch.setenv("DB_DATABASE", "school_db")
    monkeypatch.setenv("DB_PASSWORD", "must-not-appear-in-process-arguments")

    edutrack.backup.run_daily_backup()

    command = commands[0]
    assert command[1] == f"--defaults-extra-file={option_file}"
    assert "--single-transaction" in command
    assert "--no-tablespaces" in command
    assert not any(argument.startswith("-p") for argument in command)
    assert "must-not-appear-in-process-arguments" not in " ".join(command)
    assert not option_file.exists()
    backup = next(backup_folder.glob("*.sql.enc"))
    assert b"CREATE TABLE example" not in backup.read_bytes()


def test_daily_backup_removes_partial_output_and_option_file_on_dump_failure(
    client, monkeypatch, tmp_path, capsys
):
    backup_folder = tmp_path / "backups"
    backup_folder.mkdir()
    option_file = backup_folder / "mysqldump_opts_test.cnf"
    option_file.write_text("[client]\npassword=test-only\n", encoding="utf-8")

    class FailedProcess:
        stdout = io.BytesIO(b"partial SQL")
        returncode = 2

        def __init__(self, command, stdout):
            pass

        def wait(self):
            return self.returncode

    monkeypatch.setattr(edutrack.backup, "BACKUP_FOLDER", str(backup_folder))
    monkeypatch.setattr(edutrack.backup, "create_secure_option_file", lambda: str(option_file))
    monkeypatch.setattr(edutrack.backup.keyring, "get_password", lambda *args: "backup-test-passphrase")
    monkeypatch.setattr(edutrack.subprocess, "Popen", FailedProcess)
    monkeypatch.setenv("DB_HOST", "localhost")
    monkeypatch.setenv("DB_USERNAME", "school_app")
    monkeypatch.setenv("DB_DATABASE", "school_db")

    edutrack.backup.run_daily_backup()

    assert "Backup failed:" in capsys.readouterr().out
    assert not option_file.exists()
    assert not list(backup_folder.glob("*.sql.enc"))
