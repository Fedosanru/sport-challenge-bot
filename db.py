from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


@dataclass(slots=True)
class Challenge:
    id: int
    title: str
    start_date: str
    end_date: str
    status: str
    pushup_limit: int
    pullup_limit: int
    squat_limit: int
    results_chat_id: int | None


class Database:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}

    def init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    username TEXT,
                    reminders_enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS challenges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active', 'finished')),
                    pushup_limit INTEGER NOT NULL DEFAULT 200,
                    pullup_limit INTEGER NOT NULL DEFAULT 50,
                    squat_limit INTEGER NOT NULL DEFAULT 200,
                    results_chat_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE UNIQUE INDEX IF NOT EXISTS one_active_challenge
                ON challenges(status)
                WHERE status = 'active';

                CREATE TABLE IF NOT EXISTS results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    challenge_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    result_date TEXT NOT NULL,
                    pushups INTEGER NOT NULL,
                    pullups INTEGER NOT NULL,
                    squats INTEGER NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(challenge_id, user_id, result_date),
                    FOREIGN KEY(challenge_id) REFERENCES challenges(id),
                    FOREIGN KEY(user_id) REFERENCES users(telegram_id)
                );

                CREATE TABLE IF NOT EXISTS notifications (
                    challenge_id INTEGER NOT NULL,
                    notification_key TEXT NOT NULL,
                    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(challenge_id, notification_key),
                    FOREIGN KEY(challenge_id) REFERENCES challenges(id)
                );

                CREATE TABLE IF NOT EXISTS achievements (
                    challenge_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    achievement_key TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    earned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(challenge_id, user_id, achievement_key),
                    FOREIGN KEY(challenge_id) REFERENCES challenges(id),
                    FOREIGN KEY(user_id) REFERENCES users(telegram_id)
                );
                """
            )
            # Миграция со старой версии без потери данных.
            user_columns = self._columns(conn, "users")
            if "reminders_enabled" not in user_columns:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN reminders_enabled INTEGER NOT NULL DEFAULT 1"
                )

    def upsert_user(self, telegram_id: int, full_name: str, username: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users(telegram_id, full_name, username)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    full_name = excluded.full_name,
                    username = excluded.username
                """,
                (telegram_id, full_name, username),
            )

    def toggle_reminders(self, user_id: int) -> bool:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE users
                SET reminders_enabled = CASE reminders_enabled WHEN 1 THEN 0 ELSE 1 END
                WHERE telegram_id = ?
                """,
                (user_id,),
            )
            row = conn.execute(
                "SELECT reminders_enabled FROM users WHERE telegram_id = ?",
                (user_id,),
            ).fetchone()
        return bool(row and row["reminders_enabled"])

    def get_active_challenge(self) -> Challenge | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, title, start_date, end_date, status,
                       pushup_limit, pullup_limit, squat_limit, results_chat_id
                FROM challenges WHERE status = 'active' LIMIT 1
                """
            ).fetchone()
        return Challenge(**dict(row)) if row else None

    def create_challenge(
        self,
        title: str,
        start_date: str,
        end_date: str,
        results_chat_id: int | None,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO challenges(
                    title, start_date, end_date, status, results_chat_id
                ) VALUES (?, ?, ?, 'active', ?)
                """,
                (title, start_date, end_date, results_chat_id),
            )
            return int(cursor.lastrowid)

    def finish_challenge(self, challenge_id: int) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT results_chat_id FROM challenges WHERE id = ?",
                (challenge_id,),
            ).fetchone()
            conn.execute(
                "UPDATE challenges SET status = 'finished' WHERE id = ?",
                (challenge_id,),
            )
        return row["results_chat_id"] if row else None

    def save_result(
        self,
        challenge_id: int,
        user_id: int,
        result_date: str,
        pushups: int,
        pullups: int,
        squats: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO results(
                    challenge_id, user_id, result_date, pushups, pullups, squats
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(challenge_id, user_id, result_date) DO UPDATE SET
                    pushups = excluded.pushups,
                    pullups = excluded.pullups,
                    squats = excluded.squats,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (challenge_id, user_id, result_date, pushups, pullups, squats),
            )

    def get_result(
        self, challenge_id: int, user_id: int, result_date: str
    ) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT pushups, pullups, squats
                FROM results
                WHERE challenge_id = ? AND user_id = ? AND result_date = ?
                """,
                (challenge_id, user_id, result_date),
            ).fetchone()

    def get_all_users(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT telegram_id, full_name, username, reminders_enabled
                FROM users ORDER BY full_name
                """
            ).fetchall()

    def get_missing_users(
        self, challenge_id: int, result_date: str, reminders_only: bool = False
    ) -> list[sqlite3.Row]:
        condition = "AND u.reminders_enabled = 1" if reminders_only else ""
        with self._connect() as conn:
            return conn.execute(
                f"""
                SELECT u.telegram_id, u.full_name, u.username
                FROM users u
                WHERE NOT EXISTS (
                    SELECT 1 FROM results r
                    WHERE r.challenge_id = ?
                      AND r.user_id = u.telegram_id
                      AND r.result_date = ?
                )
                {condition}
                ORDER BY u.full_name
                """,
                (challenge_id, result_date),
            ).fetchall()

    def get_daily_results(
        self, challenge_id: int, result_date: str
    ) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT u.full_name, u.username,
                       r.pushups, r.pullups, r.squats,
                       (
                         CAST(r.pushups AS REAL) / c.pushup_limit +
                         CAST(r.pullups AS REAL) / c.pullup_limit +
                         CAST(r.squats AS REAL) / c.squat_limit
                       ) AS points
                FROM results r
                JOIN users u ON u.telegram_id = r.user_id
                JOIN challenges c ON c.id = r.challenge_id
                WHERE r.challenge_id = ? AND r.result_date = ?
                ORDER BY points DESC, u.full_name
                """,
                (challenge_id, result_date),
            ).fetchall()

    def get_ranking(
        self,
        challenge_id: int,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[sqlite3.Row]:
        date_sql = ""
        params: list[object] = [challenge_id]
        if date_from:
            date_sql += " AND r.result_date >= ?"
            params.append(date_from)
        if date_to:
            date_sql += " AND r.result_date <= ?"
            params.append(date_to)
        params.append(challenge_id)

        with self._connect() as conn:
            return conn.execute(
                f"""
                SELECT
                    u.telegram_id,
                    u.full_name,
                    COUNT(r.id) AS days,
                    COALESCE(SUM(r.pushups), 0) AS pushups,
                    COALESCE(SUM(r.pullups), 0) AS pullups,
                    COALESCE(SUM(r.squats), 0) AS squats,
                    COALESCE(SUM(
                        CAST(r.pushups AS REAL) / c.pushup_limit +
                        CAST(r.pullups AS REAL) / c.pullup_limit +
                        CAST(r.squats AS REAL) / c.squat_limit
                    ), 0) AS points,
                    COALESCE(SUM(
                        CASE WHEN
                            r.pushups = c.pushup_limit AND
                            r.pullups = c.pullup_limit AND
                            r.squats = c.squat_limit
                        THEN 1 ELSE 0 END
                    ), 0) AS perfect_days
                FROM users u
                LEFT JOIN results r
                    ON r.user_id = u.telegram_id
                    AND r.challenge_id = ?
                    {date_sql}
                JOIN challenges c ON c.id = ?
                GROUP BY u.telegram_id, u.full_name
                HAVING COUNT(r.id) > 0
                ORDER BY points DESC, perfect_days DESC, pullups DESC, u.full_name
                """,
                params,
            ).fetchall()

    def get_user_stats(self, challenge_id: int, user_id: int) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT
                    COUNT(r.id) AS days,
                    COALESCE(SUM(r.pushups), 0) AS pushups,
                    COALESCE(SUM(r.pullups), 0) AS pullups,
                    COALESCE(SUM(r.squats), 0) AS squats,
                    COALESCE(SUM(
                        CAST(r.pushups AS REAL) / c.pushup_limit +
                        CAST(r.pullups AS REAL) / c.pullup_limit +
                        CAST(r.squats AS REAL) / c.squat_limit
                    ), 0) AS points,
                    COALESCE(MAX(
                        CAST(r.pushups AS REAL) / c.pushup_limit +
                        CAST(r.pullups AS REAL) / c.pullup_limit +
                        CAST(r.squats AS REAL) / c.squat_limit
                    ), 0) AS best_day
                FROM challenges c
                LEFT JOIN results r
                    ON r.challenge_id = c.id AND r.user_id = ?
                WHERE c.id = ?
                GROUP BY c.id
                """,
                (user_id, challenge_id),
            ).fetchone()

    def get_user_result_dates(self, challenge_id: int, user_id: int) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT result_date FROM results
                WHERE challenge_id = ? AND user_id = ?
                ORDER BY result_date
                """,
                (challenge_id, user_id),
            ).fetchall()
        return [row["result_date"] for row in rows]

    def calculate_streaks(
        self, challenge_id: int, user_id: int, reference_date: str
    ) -> tuple[int, int]:
        dates = [date.fromisoformat(value) for value in self.get_user_result_dates(
            challenge_id, user_id
        )]
        if not dates:
            return 0, 0

        longest = 1
        running = 1
        for previous, current in zip(dates, dates[1:]):
            if current == previous + timedelta(days=1):
                running += 1
                longest = max(longest, running)
            else:
                running = 1

        ref = date.fromisoformat(reference_date)
        current_streak = 0
        cursor = ref
        date_set = set(dates)
        while cursor in date_set:
            current_streak += 1
            cursor -= timedelta(days=1)

        return current_streak, longest

    def get_all_results(self, challenge_id: int) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT r.result_date, u.full_name, u.username,
                       r.pushups, r.pullups, r.squats,
                       (
                         CAST(r.pushups AS REAL) / c.pushup_limit +
                         CAST(r.pullups AS REAL) / c.pullup_limit +
                         CAST(r.squats AS REAL) / c.squat_limit
                       ) AS points,
                       r.updated_at
                FROM results r
                JOIN users u ON u.telegram_id = r.user_id
                JOIN challenges c ON c.id = r.challenge_id
                WHERE r.challenge_id = ?
                ORDER BY r.result_date, u.full_name
                """,
                (challenge_id,),
            ).fetchall()

    def get_user_recent_results(
        self, challenge_id: int, user_id: int, limit: int = 7
    ) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT r.result_date, r.pushups, r.pullups, r.squats,
                       (
                         CAST(r.pushups AS REAL) / c.pushup_limit +
                         CAST(r.pullups AS REAL) / c.pullup_limit +
                         CAST(r.squats AS REAL) / c.squat_limit
                       ) AS points
                FROM results r
                JOIN challenges c ON c.id = r.challenge_id
                WHERE r.challenge_id = ? AND r.user_id = ?
                ORDER BY r.result_date DESC
                LIMIT ?
                """,
                (challenge_id, user_id, limit),
            ).fetchall()

    def get_user_rank(self, challenge_id: int, user_id: int) -> tuple[int | None, int]:
        rows = self.get_ranking(challenge_id)
        for index, row in enumerate(rows, start=1):
            if row["telegram_id"] == user_id:
                return index, len(rows)
        return None, len(rows)

    def award_achievement(
        self, challenge_id: int, user_id: int, key: str, title: str, description: str
    ) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO achievements(
                    challenge_id, user_id, achievement_key, title, description
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (challenge_id, user_id, key, title, description),
            )
            return cursor.rowcount > 0

    def get_user_achievements(
        self, challenge_id: int, user_id: int
    ) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT achievement_key, title, description, earned_at
                FROM achievements
                WHERE challenge_id = ? AND user_id = ?
                ORDER BY earned_at, achievement_key
                """,
                (challenge_id, user_id),
            ).fetchall()

    def notification_was_sent(self, challenge_id: int, key: str) -> bool:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT 1 FROM notifications
                WHERE challenge_id = ? AND notification_key = ?
                """,
                (challenge_id, key),
            ).fetchone() is not None

    def mark_notification_sent(self, challenge_id: int, key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO notifications(challenge_id, notification_key)
                VALUES (?, ?)
                """,
                (challenge_id, key),
            )

    def get_expired_active(self, today: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT id, title, results_chat_id
                FROM challenges
                WHERE status = 'active' AND end_date < ?
                LIMIT 1
                """,
                (today,),
            ).fetchone()
