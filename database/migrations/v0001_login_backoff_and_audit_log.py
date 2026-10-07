def upgrade(cursor):
    cursor.execute(
        """
        SELECT COUNT(*) AS table_count
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users'
        """
    )
    if cursor.fetchone()["table_count"] == 0:
        raise RuntimeError(
            "The users table is missing. Initialize a new database from "
            "database/schema.sql before running migrations."
        )

    cursor.execute(
        """
        SELECT COUNT(*) AS column_count
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'users'
          AND COLUMN_NAME = 'failed_login_attempts'
        """
    )
    if cursor.fetchone()["column_count"] == 0:
        cursor.execute(
            "ALTER TABLE users ADD COLUMN failed_login_attempts "
            "INT NOT NULL DEFAULT 0"
        )

    cursor.execute(
        """
        SELECT COUNT(*) AS column_count
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'users'
          AND COLUMN_NAME = 'locked_until'
        """
    )
    if cursor.fetchone()["column_count"] == 0:
        cursor.execute(
            "ALTER TABLE users ADD COLUMN locked_until DATETIME DEFAULT NULL"
        )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS `audit_log` (
          `log_id` bigint NOT NULL AUTO_INCREMENT,
          `user_id` int DEFAULT NULL,
          `username` varchar(50) NOT NULL,
          `action` varchar(50) NOT NULL,
          `entity_type` varchar(50) NOT NULL,
          `entity_id` int DEFAULT NULL,
          `details` json DEFAULT NULL,
          `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (`log_id`),
          KEY `idx_audit_created_at` (`created_at`),
          KEY `idx_audit_entity` (`entity_type`,`entity_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        COLLATE=utf8mb4_0900_ai_ci
        """
    )
