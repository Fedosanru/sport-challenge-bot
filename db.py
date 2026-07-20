from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


@dataclass(slots=True)
class Group:
    id: int
    telegram_chat_id: int
    title: str


@dataclass(slots=True)
class Challenge:
    id: int
    group_id: int
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

    def _cols(self, conn: sqlite3.Connection, table: str) -> set[str]:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    def init(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS users(
                telegram_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                username TEXT,
                reminders_enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS groups(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_chat_id INTEGER NOT NULL UNIQUE,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS group_members(
                group_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(group_id,user_id),
                FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS user_settings(
                user_id INTEGER PRIMARY KEY,
                selected_group_id INTEGER,
                FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                FOREIGN KEY(selected_group_id) REFERENCES groups(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS challenges(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER,
                title TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active','finished')),
                pushup_limit INTEGER NOT NULL DEFAULT 200,
                pullup_limit INTEGER NOT NULL DEFAULT 50,
                squat_limit INTEGER NOT NULL DEFAULT 200,
                results_chat_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS results(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                challenge_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                result_date TEXT NOT NULL,
                pushups INTEGER NOT NULL,
                pullups INTEGER NOT NULL,
                squats INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(challenge_id,user_id,result_date),
                FOREIGN KEY(challenge_id) REFERENCES challenges(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS achievements(
                challenge_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                achievement_key TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                earned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(challenge_id,user_id,achievement_key)
            );
            CREATE TABLE IF NOT EXISTS notifications(
                challenge_id INTEGER NOT NULL,
                notification_key TEXT NOT NULL,
                sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(challenge_id,notification_key)
            );
            """)
            # Миграция v5: добавляем group_id и переносим старый челлендж в legacy-группу.
            if "group_id" not in self._cols(conn, "challenges"):
                conn.execute("ALTER TABLE challenges ADD COLUMN group_id INTEGER")
            conn.execute("DROP INDEX IF EXISTS one_active_challenge")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_challenge_per_group ON challenges(group_id) WHERE status='active'")
            legacy = conn.execute("SELECT id FROM groups WHERE telegram_chat_id=-1").fetchone()
            if conn.execute("SELECT 1 FROM challenges WHERE group_id IS NULL LIMIT 1").fetchone():
                if legacy is None:
                    cur = conn.execute("INSERT INTO groups(telegram_chat_id,title) VALUES(-1,'Старая группа')")
                    legacy_id = int(cur.lastrowid)
                else:
                    legacy_id = int(legacy["id"])
                conn.execute("UPDATE challenges SET group_id=? WHERE group_id IS NULL", (legacy_id,))
                conn.execute("INSERT OR IGNORE INTO group_members(group_id,user_id) SELECT ?,telegram_id FROM users", (legacy_id,))

    def upsert_user(self, telegram_id:int, full_name:str, username:str|None)->None:
        with self._connect() as conn:
            conn.execute("""INSERT INTO users(telegram_id,full_name,username) VALUES(?,?,?)
            ON CONFLICT(telegram_id) DO UPDATE SET full_name=excluded.full_name, username=excluded.username""",(telegram_id,full_name,username))

    def upsert_group(self, chat_id:int, title:str)->Group:
        with self._connect() as conn:
            conn.execute("""INSERT INTO groups(telegram_chat_id,title) VALUES(?,?)
            ON CONFLICT(telegram_chat_id) DO UPDATE SET title=excluded.title""",(chat_id,title))
            row=conn.execute("SELECT id,telegram_chat_id,title FROM groups WHERE telegram_chat_id=?",(chat_id,)).fetchone()
        return Group(**dict(row))

    def get_group_by_chat(self, chat_id:int)->Group|None:
        with self._connect() as conn:
            row=conn.execute("SELECT id,telegram_chat_id,title FROM groups WHERE telegram_chat_id=?",(chat_id,)).fetchone()
        return Group(**dict(row)) if row else None

    def get_group(self, group_id:int)->Group|None:
        with self._connect() as conn:
            row=conn.execute("SELECT id,telegram_chat_id,title FROM groups WHERE id=?",(group_id,)).fetchone()
        return Group(**dict(row)) if row else None

    def add_member(self, group_id:int, user_id:int, role:str='member')->None:
        with self._connect() as conn:
            conn.execute("INSERT INTO group_members(group_id,user_id,role) VALUES(?,?,?) ON CONFLICT(group_id,user_id) DO UPDATE SET role=CASE WHEN group_members.role='admin' THEN 'admin' ELSE excluded.role END",(group_id,user_id,role))
            conn.execute("INSERT INTO user_settings(user_id,selected_group_id) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET selected_group_id=COALESCE(user_settings.selected_group_id,excluded.selected_group_id)",(user_id,group_id))

    def get_user_groups(self,user_id:int)->list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("""SELECT g.id,g.telegram_chat_id,g.title,gm.role,
            EXISTS(SELECT 1 FROM challenges c WHERE c.group_id=g.id AND c.status='active') AS has_active
            FROM group_members gm JOIN groups g ON g.id=gm.group_id WHERE gm.user_id=? ORDER BY g.title""",(user_id,)).fetchall()

    def set_selected_group(self,user_id:int,group_id:int)->None:
        with self._connect() as conn:
            conn.execute("INSERT INTO user_settings(user_id,selected_group_id) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET selected_group_id=excluded.selected_group_id",(user_id,group_id))

    def get_selected_group(self,user_id:int)->Group|None:
        with self._connect() as conn:
            row=conn.execute("""SELECT g.id,g.telegram_chat_id,g.title FROM user_settings s JOIN groups g ON g.id=s.selected_group_id JOIN group_members gm ON gm.group_id=g.id AND gm.user_id=s.user_id WHERE s.user_id=?""",(user_id,)).fetchone()
        return Group(**dict(row)) if row else None

    def is_group_admin(self,group_id:int,user_id:int)->bool:
        with self._connect() as conn:
            row=conn.execute("SELECT role FROM group_members WHERE group_id=? AND user_id=?",(group_id,user_id)).fetchone()
        return bool(row and row['role']=='admin')

    def get_active_challenge(self,group_id:int)->Challenge|None:
        with self._connect() as conn:
            row=conn.execute("SELECT id,group_id,title,start_date,end_date,status,pushup_limit,pullup_limit,squat_limit,results_chat_id FROM challenges WHERE group_id=? AND status='active' LIMIT 1",(group_id,)).fetchone()
        return Challenge(**dict(row)) if row else None

    def create_challenge(self,group_id:int,title:str,start_date:str,end_date:str,results_chat_id:int|None)->int:
        with self._connect() as conn:
            cur=conn.execute("INSERT INTO challenges(group_id,title,start_date,end_date,status,results_chat_id) VALUES(?,?,?,?,'active',?)",(group_id,title,start_date,end_date,results_chat_id))
            return int(cur.lastrowid)

    def finish_challenge(self,challenge_id:int)->int|None:
        with self._connect() as conn:
            row=conn.execute("SELECT results_chat_id FROM challenges WHERE id=?",(challenge_id,)).fetchone()
            conn.execute("UPDATE challenges SET status='finished' WHERE id=?",(challenge_id,))
        return row['results_chat_id'] if row else None

    def save_result(self,challenge_id:int,user_id:int,result_date:str,pushups:int,pullups:int,squats:int)->None:
        with self._connect() as conn:
            conn.execute("""INSERT INTO results(challenge_id,user_id,result_date,pushups,pullups,squats) VALUES(?,?,?,?,?,?)
            ON CONFLICT(challenge_id,user_id,result_date) DO UPDATE SET pushups=excluded.pushups,pullups=excluded.pullups,squats=excluded.squats,updated_at=CURRENT_TIMESTAMP""",(challenge_id,user_id,result_date,pushups,pullups,squats))

    def get_result(self,challenge_id:int,user_id:int,result_date:str):
        with self._connect() as conn:
            return conn.execute("SELECT pushups,pullups,squats FROM results WHERE challenge_id=? AND user_id=? AND result_date=?",(challenge_id,user_id,result_date)).fetchone()

    def get_ranking(self,challenge_id:int)->list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("""SELECT u.telegram_id,u.full_name,COUNT(r.id) days,SUM(r.pushups) pushups,SUM(r.pullups) pullups,SUM(r.squats) squats,
            SUM(CAST(r.pushups AS REAL)/c.pushup_limit+CAST(r.pullups AS REAL)/c.pullup_limit+CAST(r.squats AS REAL)/c.squat_limit) points,
            SUM(CASE WHEN r.pushups=c.pushup_limit AND r.pullups=c.pullup_limit AND r.squats=c.squat_limit THEN 1 ELSE 0 END) perfect_days
            FROM results r JOIN users u ON u.telegram_id=r.user_id JOIN challenges c ON c.id=r.challenge_id WHERE r.challenge_id=?
            GROUP BY u.telegram_id,u.full_name ORDER BY points DESC,perfect_days DESC,pullups DESC,u.full_name""",(challenge_id,)).fetchall()

    def get_group_stats(self,challenge_id:int,today:str)->sqlite3.Row:
        with self._connect() as conn:
            return conn.execute("""SELECT
            (SELECT COUNT(*) FROM group_members gm JOIN challenges c2 ON c2.group_id=gm.group_id WHERE c2.id=?) members,
            COUNT(DISTINCT CASE WHEN r.result_date=? THEN r.user_id END) active_today,
            COUNT(r.id) result_days,
            COALESCE(SUM(r.pushups),0) pushups,COALESCE(SUM(r.pullups),0) pullups,COALESCE(SUM(r.squats),0) squats,
            COALESCE(SUM(CAST(r.pushups AS REAL)/c.pushup_limit+CAST(r.pullups AS REAL)/c.pullup_limit+CAST(r.squats AS REAL)/c.squat_limit),0) points,
            COALESCE(SUM(CASE WHEN r.pushups=c.pushup_limit AND r.pullups=c.pullup_limit AND r.squats=c.squat_limit THEN 1 ELSE 0 END),0) perfect_days
            FROM challenges c LEFT JOIN results r ON r.challenge_id=c.id WHERE c.id=?""",(challenge_id,today,challenge_id)).fetchone()

    def get_user_stats(self,challenge_id:int,user_id:int)->sqlite3.Row:
        with self._connect() as conn:
            return conn.execute("""SELECT COUNT(r.id) days,COALESCE(SUM(r.pushups),0) pushups,COALESCE(SUM(r.pullups),0) pullups,COALESCE(SUM(r.squats),0) squats,
            COALESCE(SUM(CAST(r.pushups AS REAL)/c.pushup_limit+CAST(r.pullups AS REAL)/c.pullup_limit+CAST(r.squats AS REAL)/c.squat_limit),0) points
            FROM challenges c LEFT JOIN results r ON r.challenge_id=c.id AND r.user_id=? WHERE c.id=?""",(user_id,challenge_id)).fetchone()

    def get_user_result_dates(self,challenge_id:int,user_id:int)->list[str]:
        with self._connect() as conn:
            return [r['result_date'] for r in conn.execute("SELECT result_date FROM results WHERE challenge_id=? AND user_id=? ORDER BY result_date",(challenge_id,user_id)).fetchall()]

    def calculate_streaks(self,challenge_id:int,user_id:int,reference_date:str)->tuple[int,int]:
        dates=[date.fromisoformat(v) for v in self.get_user_result_dates(challenge_id,user_id)]
        if not dates:return 0,0
        longest=running=1
        for prev,cur in zip(dates,dates[1:]):
            running=running+1 if cur==prev+timedelta(days=1) else 1
            longest=max(longest,running)
        cursor=date.fromisoformat(reference_date); s=set(dates); current=0
        while cursor in s: current+=1; cursor-=timedelta(days=1)
        return current,longest

    def toggle_reminders(self,user_id:int)->bool:
        with self._connect() as conn:
            conn.execute("UPDATE users SET reminders_enabled=CASE reminders_enabled WHEN 1 THEN 0 ELSE 1 END WHERE telegram_id=?",(user_id,))
            row=conn.execute("SELECT reminders_enabled FROM users WHERE telegram_id=?",(user_id,)).fetchone()
        return bool(row and row['reminders_enabled'])
