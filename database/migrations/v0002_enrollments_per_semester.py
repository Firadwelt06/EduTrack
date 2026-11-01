def _index_exists(cursor, index_name):
    cursor.execute(
        """
        SELECT COUNT(*) AS index_count
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'enrollments'
          AND INDEX_NAME = %s
        """,
        (index_name,),
    )
    return cursor.fetchone()["index_count"] > 0


def upgrade(cursor):
    cursor.execute(
        """
        SELECT COUNT(*) AS table_count
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'enrollments'
        """
    )
    if cursor.fetchone()["table_count"] == 0:
        raise RuntimeError(
            "The enrollments table is missing. Initialize the EduTrack "
            "database before running migrations."
        )

    if _index_exists(cursor, "unique_enrollment_period"):
        if _index_exists(cursor, "unique_enrollment"):
            cursor.execute("ALTER TABLE enrollments DROP INDEX unique_enrollment")
        return

    if _index_exists(cursor, "unique_enrollment"):
        cursor.execute("ALTER TABLE enrollments DROP INDEX unique_enrollment")

    cursor.execute(
        """
        CREATE UNIQUE INDEX unique_enrollment_period
        ON enrollments (student_id, course_id, semester_id)
        """
    )
