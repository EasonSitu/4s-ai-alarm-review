import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import STATE, callback_ready, model_ready
from .contracts import CALLBACK_DELAYS, REVIEW_DELAYS, RULE, Task, local_review_model


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@contextmanager
def db(mode):
    if mode != "live":
        raise ValueError("未知环境")
    conn = sqlite3.connect(STATE / mode / "tasks.sqlite3", timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA synchronous=FULL")
        with conn:
            yield conn
    finally:
        conn.close()


def init(mode, cfg):
    (STATE / mode).mkdir(exist_ok=True, mode=0o700)
    with db(mode) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, alert_id TEXT NOT NULL,
            alarm_type TEXT NOT NULL, occurred_at TEXT, camera_id TEXT, roi TEXT,
            signature TEXT NOT NULL, received_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
            status TEXT NOT NULL, verdict TEXT, reason TEXT, error TEXT, model TEXT, elapsed_ms INTEGER,
            rule_version TEXT NOT NULL, review_attempts INTEGER NOT NULL DEFAULT 0,
            review_due REAL NOT NULL DEFAULT 0, callback_status TEXT NOT NULL DEFAULT 'waiting_result',
            callback_attempts INTEGER NOT NULL DEFAULT 0, callback_due REAL NOT NULL DEFAULT 0,
            callback_error TEXT, UNIQUE(project_id,alert_id)
        );
        CREATE TABLE IF NOT EXISTS images (task_id TEXT PRIMARY KEY REFERENCES tasks, mime TEXT NOT NULL, data BLOB NOT NULL);
        CREATE INDEX IF NOT EXISTS review_queue ON tasks(status, review_due, received_at);
        CREATE INDEX IF NOT EXISTS delivery_queue ON tasks(callback_status, callback_due, received_at);
        CREATE INDEX IF NOT EXISTS received ON tasks(received_at);
        """)
        c.execute("BEGIN IMMEDIATE")
        if "person_boxes" not in {r[1] for r in c.execute("PRAGMA table_info(tasks)")}:
            c.execute("ALTER TABLE tasks ADD COLUMN person_boxes TEXT")
        if "historical_test" not in {r[1] for r in c.execute("PRAGMA table_info(tasks)")}:
            c.execute("ALTER TABLE tasks ADD COLUMN historical_test INTEGER NOT NULL DEFAULT 0")
            c.execute("""UPDATE tasks SET historical_test=1
                         WHERE project_id GLOB 'test-*' OR project_id='vision-test-4s'""")
        c.execute("CREATE TABLE IF NOT EXISTS callback_config (id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL)")
        c.execute("""CREATE TABLE IF NOT EXISTS review_history(
            task_id TEXT NOT NULL REFERENCES tasks, attempt INTEGER NOT NULL, profile TEXT, endpoint_origin TEXT,
            requested_model TEXT, rule_version TEXT, started_at TEXT, finished_at TEXT, status TEXT, actual_model TEXT,
            PRIMARY KEY(task_id,attempt))""")
        c.execute("UPDATE review_history SET status='interrupted',finished_at=? WHERE status='processing'", (now(),))
        callback = "pending" if callback_ready(cfg, mode) else "unconfigured"
        c.execute("""UPDATE tasks SET status='failed', error='最后一次审核中断', finished_at=?, callback_status=?
                   WHERE historical_test=0 AND status='processing' AND review_attempts>=?""", (now(),callback,len(REVIEW_DELAYS)+1))
        c.execute("""UPDATE tasks SET callback_status='failed', callback_error='最后一次发送中断，未收到确认'
                   WHERE historical_test=0 AND callback_status='sending' AND callback_attempts>=?""", (len(CALLBACK_DELAYS)+1,))
        c.execute("UPDATE tasks SET status='queued' WHERE historical_test=0 AND status='processing'")
        c.execute("UPDATE tasks SET callback_status='pending' WHERE historical_test=0 AND callback_status='sending'")
        c.execute("DROP VIEW IF EXISTS pilot_tasks")
        c.execute("CREATE VIEW pilot_tasks AS SELECT * FROM tasks WHERE historical_test=0")
        if model_ready(cfg):
            c.execute("UPDATE tasks SET status='queued' WHERE historical_test=0 AND status='awaiting_model'")
        else:
            c.execute("""UPDATE tasks SET status='awaiting_model' WHERE status IN ('queued','retrying')
                         AND alarm_type IN ('no_helmet','no_vest') AND historical_test=0""")
        fingerprint = hashlib.sha256(json.dumps([cfg["callback_url"], cfg["callback_token"]]).encode()).hexdigest()
        previous = c.execute("SELECT fingerprint FROM callback_config WHERE id=1").fetchone()
        if callback_ready(cfg, mode) and (previous is None or previous[0] != fingerprint):
            c.execute("""UPDATE tasks SET callback_status='pending',callback_attempts=0,callback_due=0,
                         callback_error=NULL WHERE historical_test=0 AND callback_status IN ('failed','retrying','pending','unconfigured')""")
        c.execute("INSERT OR REPLACE INTO callback_config VALUES(1,?)", (fingerprint,))
        if callback_ready(cfg, mode):
            c.execute("UPDATE tasks SET callback_status='pending' WHERE historical_test=0 AND callback_status='unconfigured'")
        else:
            c.execute("UPDATE tasks SET callback_status='unconfigured' WHERE historical_test=0 AND callback_status IN ('retrying','pending')")


class Conflict(Exception):
    pass


def add(mode, cfg, meta, data, mime, signature):
    with db(mode) as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM tasks WHERE project_id=? AND alert_id=?", (meta["project_id"], meta["alert_id"])).fetchone()
        if row:
            if row["signature"] != signature:
                raise Conflict
            return view(mode, row), False
        task_id = uuid.uuid4().hex
        model = local_review_model(mode, meta["project_id"], meta["alarm_type"])
        external = model is None
        waiting = external and not model_ready(cfg)
        c.execute("""INSERT INTO tasks(task_id,project_id,alert_id,alarm_type,occurred_at,camera_id,roi,
                   signature,received_at,status,rule_version,person_boxes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (task_id, meta["project_id"], meta["alert_id"], meta["alarm_type"], meta["occurred_at"],
                   meta["camera_id"], json.dumps(meta["roi"]), signature, now(), "awaiting_model" if waiting else "queued", RULE, json.dumps(meta.get("person_boxes"))))
        c.execute("INSERT INTO images VALUES(?,?,?)", (task_id, mime, data))
        return view(mode, c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()), True


def view(mode, row):
    data = dict(row)
    data["person_boxes"] = json.loads(data["person_boxes"]) if data["person_boxes"] else None
    data.update(mode=mode, roi=json.loads(data["roi"]))
    return Task.model_validate(data).model_dump()


def get(mode, task_id):
    with db(mode) as c:
        row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return view(mode, row) if row else None
