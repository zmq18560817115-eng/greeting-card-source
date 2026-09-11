"""SQLite 访问层。每次调用打开一个短连接，WAL 模式，天然支持多线程（Web + 调度器）。"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from .settings import DB_PATH, TZ

SCHEMA = """
PRAGMA journal_mode=WAL;

-- 员工名单（本地数据库为准，可从飞书通讯录同步补齐）
CREATE TABLE IF NOT EXISTS employees (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    employee_no     TEXT,                       -- 工号，可空
    email           TEXT,
    department      TEXT,
    feishu_user_id  TEXT,                       -- 飞书 user_id
    feishu_open_id  TEXT,                       -- 飞书 open_id（推送用）
    join_date       TEXT,                       -- YYYY-MM-DD 入职日期
    birth_date      TEXT,                       -- YYYY-MM-DD 生日（年份可为 1900 占位）
    gender          TEXT,
    active          INTEGER NOT NULL DEFAULT 1, -- 0=离职/停用
    source          TEXT    DEFAULT 'local',    -- local / feishu
    note            TEXT,
    created_at      TEXT,
    updated_at      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_emp_openid ON employees(feishu_open_id)
    WHERE feishu_open_id IS NOT NULL AND feishu_open_id <> '';
CREATE INDEX IF NOT EXISTS idx_emp_name ON employees(name);

-- 一次「事件」= 某个员工的某一次生日 / 某一次入职周年
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id     INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    event_type      TEXT    NOT NULL,           -- birthday | anniversary
    event_date      TEXT    NOT NULL,           -- YYYY-MM-DD 当天
    trigger_at      TEXT    NOT NULL,           -- YYYY-MM-DD HH:MM:SS 计划推送时刻
    years           INTEGER,                    -- 周年数 / 年龄
    cycle_start     TEXT,                       -- 本轮周期（下周一）
    cycle_end       TEXT,
    status          TEXT    NOT NULL DEFAULT 'generating',
        -- generating 生成中 | ready 待人事确认 | confirmed 已确认待推送
        -- | pushing 推送中 | pushed 已推送 | failed 推送失败 | skipped 已跳过
    selected_card_id INTEGER,
    confirmed_by    TEXT,
    confirmed_at    TEXT,
    pushed_at       TEXT,
    last_error      TEXT,
    push_attempts   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT,
    updated_at      TEXT,
    UNIQUE(employee_id, event_type, event_date)
);
CREATE INDEX IF NOT EXISTS idx_ev_status ON events(status);
CREATE INDEX IF NOT EXISTS idx_ev_trigger ON events(trigger_at);

-- 每个事件下的候选贺卡图（默认 5 张）
CREATE TABLE IF NOT EXISTS cards (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    idx           INTEGER NOT NULL,             -- 1..5
    mode          TEXT,                         -- compose(模板合字) | ai_only
    template_key  TEXT,
    prompt        TEXT,
    bg_url        TEXT,                         -- 豆包返回的原图 URL
    bg_path       TEXT,                         -- 本地背景图
    file_path     TEXT,                         -- 最终成品图（推送用）
    status        TEXT NOT NULL DEFAULT 'ok',   -- ok | failed
    error         TEXT,
    created_at    TEXT,
    UNIQUE(event_id, idx)
);

-- 推送流水（含重推）
CREATE TABLE IF NOT EXISTS push_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    card_id     INTEGER,
    attempt     INTEGER,
    status      TEXT,                           -- success | failed
    image_key   TEXT,
    message_id  TEXT,
    error       TEXT,
    operator    TEXT,                           -- auto / 手动重推的人
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_log_event ON push_logs(event_id);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS poster_links (
    token_hash TEXT PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id),
    card_id INTEGER NOT NULL,
    delivery_uuid TEXT NOT NULL,
    app_id TEXT NOT NULL,
    open_id TEXT NOT NULL,
    expires INTEGER NOT NULL,
    UNIQUE(event_id, delivery_uuid)
);
"""


def now():
    return datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M:%S")


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with tx() as conn:
        conn.executescript(SCHEMA)
        # 增量迁移：保留已有员工、海报和流水，旧身份需要重新核验。
        migrations = {
            "employees": {
                "identity_status": "TEXT NOT NULL DEFAULT 'pending'",
                "identity_error": "TEXT", "identity_verified_at": "TEXT",
                "identity_snapshot": "TEXT",
            },
            "events": {"employee_snapshot": "TEXT", "delivery_uuid": "TEXT",
                       "generation_token": "TEXT", "worker_pid": "INTEGER",
                       "delivery_started_at": "TEXT", "notice_message_id": "TEXT",
                       "notice_delivery_uuid": "TEXT", "notice_app_id": "TEXT",
                       "notice_open_id": "TEXT", "notice_sent_at": "TEXT", "confirmed_app_id": "TEXT",
                       "delivery_mode": "TEXT", "reviewed_at": "TEXT", "reviewed_by": "TEXT"},
            "cards": {"employee_snapshot": "TEXT"},
            "push_logs": {"recipient_snapshot": "TEXT"},
        }
        for table, columns in migrations.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, definition in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def query(sql, params=()):
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def query_one(sql, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql, params=()):
    with tx() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid


def get_kv(key, default=None):
    row = query_one("SELECT value FROM kv WHERE key=?", (key,))
    return row["value"] if row else default


def set_kv(key, value):
    execute(
        "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
