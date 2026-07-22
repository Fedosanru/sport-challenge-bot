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
    results_chat_id: int | None
    scoring_mode: str = 'proportional'
    over_target_mode: str = 'stats_only'
    success_mode: str = 'all_targets'
    min_daily_points: float = 0
    edit_days: int = 1
    join_mode: str = 'open'
    rules_locked: int = 1


@dataclass(slots=True)
class Exercise:
    id: int
    challenge_id: int
    name: str
    unit: str
    daily_target: float
    max_points: float
    sort_order: int


class Database:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
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
                PRIMARY KEY(group_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS user_settings(
                user_id INTEGER PRIMARY KEY,
                selected_group_id INTEGER
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
                scoring_mode TEXT NOT NULL DEFAULT 'proportional',
                over_target_mode TEXT NOT NULL DEFAULT 'stats_only',
                success_mode TEXT NOT NULL DEFAULT 'all_targets',
                min_daily_points REAL NOT NULL DEFAULT 0,
                edit_days INTEGER NOT NULL DEFAULT 1,
                join_mode TEXT NOT NULL DEFAULT 'open',
                rules_locked INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS results(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                challenge_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                result_date TEXT NOT NULL,
                pushups INTEGER NOT NULL DEFAULT 0,
                pullups INTEGER NOT NULL DEFAULT 0,
                squats INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(challenge_id,user_id,result_date)
            );
            CREATE TABLE IF NOT EXISTS challenge_exercises(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                challenge_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                unit TEXT NOT NULL DEFAULT 'раз',
                daily_target REAL NOT NULL,
                max_points REAL NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(challenge_id) REFERENCES challenges(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS result_values(
                result_id INTEGER NOT NULL,
                exercise_id INTEGER NOT NULL,
                value REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(result_id, exercise_id),
                FOREIGN KEY(result_id) REFERENCES results(id) ON DELETE CASCADE,
                FOREIGN KEY(exercise_id) REFERENCES challenge_exercises(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS achievements(
                challenge_id INTEGER NOT NULL,user_id INTEGER NOT NULL,achievement_key TEXT NOT NULL,
                title TEXT NOT NULL,description TEXT NOT NULL,earned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(challenge_id,user_id,achievement_key)
            );
            CREATE TABLE IF NOT EXISTS notifications(
                challenge_id INTEGER NOT NULL,notification_key TEXT NOT NULL,sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(challenge_id,notification_key)
            );
            """)
            if "group_id" not in self._cols(conn, "challenges"):
                conn.execute("ALTER TABLE challenges ADD COLUMN group_id INTEGER")
            challenge_columns = self._cols(conn, "challenges")
            rule_columns = {
                "scoring_mode": "TEXT NOT NULL DEFAULT 'proportional'",
                "over_target_mode": "TEXT NOT NULL DEFAULT 'stats_only'",
                "success_mode": "TEXT NOT NULL DEFAULT 'all_targets'",
                "min_daily_points": "REAL NOT NULL DEFAULT 0",
                "edit_days": "INTEGER NOT NULL DEFAULT 1",
                "join_mode": "TEXT NOT NULL DEFAULT 'open'",
                "rules_locked": "INTEGER NOT NULL DEFAULT 1",
            }
            for name, definition in rule_columns.items():
                if name not in challenge_columns:
                    conn.execute(f"ALTER TABLE challenges ADD COLUMN {name} {definition}")
            conn.execute("DROP INDEX IF EXISTS one_active_challenge")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_challenge_per_group ON challenges(group_id) WHERE status='active'")

            legacy = conn.execute("SELECT id FROM groups WHERE telegram_chat_id=-1").fetchone()
            if conn.execute("SELECT 1 FROM challenges WHERE group_id IS NULL LIMIT 1").fetchone():
                legacy_id = int(legacy["id"]) if legacy else int(conn.execute("INSERT INTO groups(telegram_chat_id,title) VALUES(-1,'Старая группа')").lastrowid)
                conn.execute("UPDATE challenges SET group_id=? WHERE group_id IS NULL", (legacy_id,))
                conn.execute("INSERT OR IGNORE INTO group_members(group_id,user_id) SELECT ?,telegram_id FROM users", (legacy_id,))

            # Миграция v10 -> v11: создаём универсальные упражнения для старых челленджей.
            for ch in conn.execute("SELECT id,pushup_limit,pullup_limit,squat_limit FROM challenges").fetchall():
                if not conn.execute("SELECT 1 FROM challenge_exercises WHERE challenge_id=?", (ch["id"],)).fetchone():
                    defs = [
                        ("Отжимания", "раз", ch["pushup_limit"], 1.0, 1),
                        ("Подтягивания", "раз", ch["pullup_limit"], 1.0, 2),
                        ("Приседания", "раз", ch["squat_limit"], 1.0, 3),
                    ]
                    ex_ids = []
                    for name, unit, target, points, order in defs:
                        cur = conn.execute("INSERT INTO challenge_exercises(challenge_id,name,unit,daily_target,max_points,sort_order) VALUES(?,?,?,?,?,?)",
                                           (ch["id"], name, unit, target, points, order))
                        ex_ids.append(int(cur.lastrowid))
                    for r in conn.execute("SELECT id,pushups,pullups,squats FROM results WHERE challenge_id=?", (ch["id"],)).fetchall():
                        for ex_id, value in zip(ex_ids, (r["pushups"], r["pullups"], r["squats"])):
                            conn.execute("INSERT OR IGNORE INTO result_values(result_id,exercise_id,value) VALUES(?,?,?)", (r["id"], ex_id, value))

    def upsert_user(self, telegram_id:int, full_name:str, username:str|None)->None:
        with self._connect() as conn:
            conn.execute("""INSERT INTO users(telegram_id,full_name,username) VALUES(?,?,?)
            ON CONFLICT(telegram_id) DO UPDATE SET full_name=excluded.full_name,username=excluded.username""",(telegram_id,full_name,username))

    def upsert_group(self, chat_id:int, title:str)->Group:
        with self._connect() as conn:
            conn.execute("""INSERT INTO groups(telegram_chat_id,title) VALUES(?,?)
            ON CONFLICT(telegram_chat_id) DO UPDATE SET title=excluded.title""",(chat_id,title))
            row=conn.execute("SELECT id,telegram_chat_id,title FROM groups WHERE telegram_chat_id=?",(chat_id,)).fetchone()
        return Group(**dict(row))

    def get_group_by_chat(self, chat_id:int)->Group|None:
        with self._connect() as conn: row=conn.execute("SELECT id,telegram_chat_id,title FROM groups WHERE telegram_chat_id=?",(chat_id,)).fetchone()
        return Group(**dict(row)) if row else None

    def get_group(self, group_id:int)->Group|None:
        with self._connect() as conn: row=conn.execute("SELECT id,telegram_chat_id,title FROM groups WHERE id=?",(group_id,)).fetchone()
        return Group(**dict(row)) if row else None

    def add_member(self,group_id:int,user_id:int,role:str='member')->None:
        with self._connect() as conn:
            conn.execute("INSERT INTO group_members(group_id,user_id,role) VALUES(?,?,?) ON CONFLICT(group_id,user_id) DO UPDATE SET role=CASE WHEN group_members.role='admin' THEN 'admin' ELSE excluded.role END",(group_id,user_id,role))
            conn.execute("INSERT INTO user_settings(user_id,selected_group_id) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET selected_group_id=COALESCE(user_settings.selected_group_id,excluded.selected_group_id)",(user_id,group_id))

    def get_user_groups(self,user_id:int):
        with self._connect() as conn:
            return conn.execute("""SELECT g.id,g.telegram_chat_id,g.title,gm.role,
            EXISTS(SELECT 1 FROM challenges c WHERE c.group_id=g.id AND c.status='active') has_active
            FROM group_members gm JOIN groups g ON g.id=gm.group_id WHERE gm.user_id=? ORDER BY g.title""",(user_id,)).fetchall()

    def set_selected_group(self,user_id:int,group_id:int)->None:
        with self._connect() as conn: conn.execute("INSERT INTO user_settings(user_id,selected_group_id) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET selected_group_id=excluded.selected_group_id",(user_id,group_id))

    def get_selected_group(self,user_id:int)->Group|None:
        with self._connect() as conn:
            row=conn.execute("""SELECT g.id,g.telegram_chat_id,g.title FROM user_settings s JOIN groups g ON g.id=s.selected_group_id
            JOIN group_members gm ON gm.group_id=g.id AND gm.user_id=s.user_id WHERE s.user_id=?""",(user_id,)).fetchone()
        return Group(**dict(row)) if row else None

    def is_group_admin(self,group_id:int,user_id:int)->bool:
        with self._connect() as conn: row=conn.execute("SELECT role FROM group_members WHERE group_id=? AND user_id=?",(group_id,user_id)).fetchone()
        return bool(row and row["role"]=="admin")

    def _challenge_from_row(self,row)->Challenge|None:
        return Challenge(**dict(row)) if row else None

    def get_challenge(self,challenge_id:int)->Challenge|None:
        with self._connect() as conn: row=conn.execute("SELECT id,group_id,title,start_date,end_date,status,results_chat_id,scoring_mode,over_target_mode,success_mode,min_daily_points,edit_days,join_mode,rules_locked FROM challenges WHERE id=?",(challenge_id,)).fetchone()
        return self._challenge_from_row(row)

    def get_active_challenge(self,group_id:int)->Challenge|None:
        with self._connect() as conn: row=conn.execute("SELECT id,group_id,title,start_date,end_date,status,results_chat_id,scoring_mode,over_target_mode,success_mode,min_daily_points,edit_days,join_mode,rules_locked FROM challenges WHERE group_id=? AND status='active' LIMIT 1",(group_id,)).fetchone()
        return self._challenge_from_row(row)

    def get_challenges(self,group_id:int,limit:int=10):
        with self._connect() as conn: return conn.execute("SELECT id,title,start_date,end_date,status FROM challenges WHERE group_id=? ORDER BY id DESC LIMIT ?",(group_id,limit)).fetchall()

    def get_exercises(self,challenge_id:int)->list[Exercise]:
        with self._connect() as conn: rows=conn.execute("SELECT id,challenge_id,name,unit,daily_target,max_points,sort_order FROM challenge_exercises WHERE challenge_id=? ORDER BY sort_order,id",(challenge_id,)).fetchall()
        return [Exercise(**dict(r)) for r in rows]

    def create_challenge(self,group_id:int,title:str,start_date:str,end_date:str,results_chat_id:int|None,exercises:list[dict],rules:dict|None=None)->int:
        rules=rules or {}
        with self._connect() as conn:
            cur=conn.execute("""INSERT INTO challenges(group_id,title,start_date,end_date,status,results_chat_id,scoring_mode,over_target_mode,success_mode,min_daily_points,edit_days,join_mode,rules_locked) VALUES(?,?,?,?,'active',?,?,?,?,?,?,?,1)""",(group_id,title,start_date,end_date,results_chat_id,rules.get('scoring_mode','proportional'),rules.get('over_target_mode','stats_only'),rules.get('success_mode','all_targets'),float(rules.get('min_daily_points',0)),int(rules.get('edit_days',1)),rules.get('join_mode','open')))
            challenge_id=int(cur.lastrowid)
            for i,ex in enumerate(exercises,1):
                conn.execute("INSERT INTO challenge_exercises(challenge_id,name,unit,daily_target,max_points,sort_order) VALUES(?,?,?,?,?,?)",
                             (challenge_id,ex['name'],ex['unit'],float(ex['target']),float(ex.get('points',1)),i))
            return challenge_id

    def clone_challenge(self,source_id:int,group_id:int,title:str,start_date:str,end_date:str,results_chat_id:int|None)->int:
        exercises=[{'name':e.name,'unit':e.unit,'target':e.daily_target,'points':e.max_points} for e in self.get_exercises(source_id)]
        source=self.get_challenge(source_id)
        rules={k:getattr(source,k) for k in ('scoring_mode','over_target_mode','success_mode','min_daily_points','edit_days','join_mode')}
        return self.create_challenge(group_id,title,start_date,end_date,results_chat_id,exercises,rules)

    def finish_challenge(self,challenge_id:int)->int|None:
        with self._connect() as conn:
            row=conn.execute("SELECT results_chat_id FROM challenges WHERE id=?",(challenge_id,)).fetchone(); conn.execute("UPDATE challenges SET status='finished' WHERE id=?",(challenge_id,))
        return row['results_chat_id'] if row else None

    def save_result(self,challenge_id:int,user_id:int,result_date:str,values:dict[int,float])->None:
        exercises = self.get_exercises(challenge_id)
        if not exercises:
            raise ValueError("У челленджа нет упражнений")
        allowed_ids = {e.id for e in exercises}
        unknown_ids = set(values) - allowed_ids
        if unknown_ids:
            raise ValueError(f"Неизвестные упражнения: {sorted(unknown_ids)}")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO results(challenge_id,user_id,result_date) VALUES(?,?,?) "
                "ON CONFLICT(challenge_id,user_id,result_date) DO UPDATE SET updated_at=CURRENT_TIMESTAMP",
                (challenge_id,user_id,result_date),
            )
            row = conn.execute(
                "SELECT id FROM results WHERE challenge_id=? AND user_id=? AND result_date=?",
                (challenge_id,user_id,result_date),
            ).fetchone()
            if not row:
                raise RuntimeError("Не удалось создать запись результата")
            result_id = int(row["id"])

            # Записываем полный набор упражнений, включая нули. Это исключает
            # остаточные значения после повторного редактирования результата.
            for exercise in exercises:
                value = float(values.get(exercise.id, 0))
                if value < 0:
                    raise ValueError("Результат не может быть отрицательным")
                conn.execute(
                    "INSERT INTO result_values(result_id,exercise_id,value) VALUES(?,?,?) "
                    "ON CONFLICT(result_id,exercise_id) DO UPDATE SET value=excluded.value",
                    (result_id, exercise.id, value),
                )
            conn.commit()

    def get_result(self,challenge_id:int,user_id:int,result_date:str)->dict[int,float]|None:
        with self._connect() as conn:
            rows=conn.execute("""SELECT rv.exercise_id,rv.value FROM results r JOIN result_values rv ON rv.result_id=r.id
            WHERE r.challenge_id=? AND r.user_id=? AND r.result_date=?""",(challenge_id,user_id,result_date)).fetchall()
        return {int(r['exercise_id']):float(r['value']) for r in rows} if rows else None

    def _score_sql(self)->str:
        return """SUM(CASE
            WHEN e.daily_target<=0 THEN 0
            WHEN c.scoring_mode='binary' THEN CASE WHEN rv.value>=e.daily_target THEN e.max_points ELSE 0 END
            WHEN c.scoring_mode='step' THEN CASE WHEN rv.value>=e.daily_target THEN e.max_points WHEN rv.value>=e.daily_target*0.5 THEN e.max_points*0.5 ELSE 0 END
            WHEN c.scoring_mode='fixed' THEN CASE WHEN rv.value>0 THEN e.max_points ELSE 0 END
            ELSE MIN(rv.value/e.daily_target,1.0)*e.max_points END)"""

    def calculate_daily_score(self,challenge_id:int,values:dict[int,float])->float:
        ch=self.get_challenge(challenge_id)
        score=0.0
        for e in self.get_exercises(challenge_id):
            value=float(values.get(e.id,0))
            if ch.scoring_mode=='binary': part=e.max_points if value>=e.daily_target else 0
            elif ch.scoring_mode=='step': part=e.max_points if value>=e.daily_target else e.max_points*0.5 if value>=e.daily_target*0.5 else 0
            elif ch.scoring_mode=='fixed': part=e.max_points if value>0 else 0
            else: part=min(value/e.daily_target,1.0)*e.max_points if e.daily_target else 0
            score+=part
        return score

    def get_ranking(self,challenge_id:int):
        with self._connect() as conn:
            return conn.execute(f"""SELECT u.telegram_id,u.full_name,COUNT(DISTINCT r.id) days,
            {self._score_sql()} points,
            COUNT(DISTINCT CASE WHEN ABS(day_score.max_score-day_score.earned)<0.0001 THEN r.id END) perfect_days
            FROM results r JOIN challenges c ON c.id=r.challenge_id JOIN users u ON u.telegram_id=r.user_id
            JOIN result_values rv ON rv.result_id=r.id JOIN challenge_exercises e ON e.id=rv.exercise_id
            LEFT JOIN (SELECT r2.id result_id,SUM(e2.max_points) max_score,
                SUM(CASE WHEN c2.scoring_mode='binary' THEN CASE WHEN rv2.value>=e2.daily_target THEN e2.max_points ELSE 0 END
                         WHEN c2.scoring_mode='step' THEN CASE WHEN rv2.value>=e2.daily_target THEN e2.max_points WHEN rv2.value>=e2.daily_target*0.5 THEN e2.max_points*0.5 ELSE 0 END
                         WHEN c2.scoring_mode='fixed' THEN CASE WHEN rv2.value>0 THEN e2.max_points ELSE 0 END
                         ELSE MIN(rv2.value/e2.daily_target,1.0)*e2.max_points END) earned
                FROM results r2 JOIN challenges c2 ON c2.id=r2.challenge_id JOIN result_values rv2 ON rv2.result_id=r2.id JOIN challenge_exercises e2 ON e2.id=rv2.exercise_id GROUP BY r2.id) day_score ON day_score.result_id=r.id
            WHERE r.challenge_id=? GROUP BY u.telegram_id,u.full_name ORDER BY points DESC,perfect_days DESC,u.full_name""",(challenge_id,)).fetchall()

    def get_totals_by_exercise(self,challenge_id:int,today:str|None=None):
        where="r.challenge_id=?"; params=[challenge_id]
        if today is not None: where+=" AND r.result_date=?"; params.append(today)
        with self._connect() as conn:
            return conn.execute(f"""SELECT e.name,e.unit,COALESCE(SUM(rv.value),0) total FROM challenge_exercises e
            LEFT JOIN result_values rv ON rv.exercise_id=e.id LEFT JOIN results r ON r.id=rv.result_id AND r.challenge_id=e.challenge_id
            WHERE e.challenge_id=? {'AND r.result_date=?' if today is not None else ''} GROUP BY e.id ORDER BY e.sort_order,e.id""",params).fetchall()

    def get_group_stats(self,challenge_id:int,today:str):
        with self._connect() as conn:
            return conn.execute(f"""SELECT
            (SELECT COUNT(*) FROM group_members gm JOIN challenges c2 ON c2.group_id=gm.group_id WHERE c2.id=?) members,
            COUNT(DISTINCT CASE WHEN r.result_date=? THEN r.user_id END) active_today,
            COUNT(DISTINCT r.id) result_days,
            COALESCE({self._score_sql()},0) points
            FROM challenges c LEFT JOIN results r ON r.challenge_id=c.id LEFT JOIN result_values rv ON rv.result_id=r.id LEFT JOIN challenge_exercises e ON e.id=rv.exercise_id
            WHERE c.id=?""",(challenge_id,today,challenge_id)).fetchone()

    def get_user_stats(self,challenge_id:int,user_id:int):
        with self._connect() as conn:
            return conn.execute(f"""SELECT COUNT(DISTINCT r.id) days,COALESCE({self._score_sql()},0) points
            FROM results r JOIN challenges c ON c.id=r.challenge_id LEFT JOIN result_values rv ON rv.result_id=r.id LEFT JOIN challenge_exercises e ON e.id=rv.exercise_id
            WHERE r.challenge_id=? AND r.user_id=?""",(challenge_id,user_id)).fetchone()

    def get_user_totals(self,challenge_id:int,user_id:int):
        with self._connect() as conn:
            return conn.execute("""SELECT e.name,e.unit,COALESCE(SUM(rv.value),0) total FROM challenge_exercises e
            LEFT JOIN result_values rv ON rv.exercise_id=e.id LEFT JOIN results r ON r.id=rv.result_id AND r.user_id=?
            WHERE e.challenge_id=? GROUP BY e.id ORDER BY e.sort_order,e.id""",(user_id,challenge_id)).fetchall()

    def get_user_rank(self,challenge_id:int,user_id:int)->tuple[int|None,int]:
        rows=self.get_ranking(challenge_id)
        for i,row in enumerate(rows,1):
            if int(row['telegram_id'])==user_id:return i,len(rows)
        return None,len(rows)

    def get_user_result_dates(self,challenge_id:int,user_id:int)->list[str]:
        with self._connect() as conn: return [r['result_date'] for r in conn.execute("SELECT result_date FROM results WHERE challenge_id=? AND user_id=? ORDER BY result_date",(challenge_id,user_id))]

    def calculate_streaks(self,challenge_id:int,user_id:int,through_date:str)->tuple[int,int]:
        dates=[date.fromisoformat(x) for x in self.get_user_result_dates(challenge_id,user_id) if x<=through_date]
        if not dates:return 0,0
        longest=cur=1
        for a,b in zip(dates,dates[1:]):
            cur=cur+1 if b-a==timedelta(days=1) else 1; longest=max(longest,cur)
        end=date.fromisoformat(through_date); current=0; s=set(dates)
        while end in s: current+=1; end-=timedelta(days=1)
        return current,longest

    def toggle_reminders(self,user_id:int)->bool:
        with self._connect() as conn:
            row=conn.execute("SELECT reminders_enabled FROM users WHERE telegram_id=?",(user_id,)).fetchone(); value=0 if row and row['reminders_enabled'] else 1
            conn.execute("UPDATE users SET reminders_enabled=? WHERE telegram_id=?",(value,user_id)); return bool(value)
