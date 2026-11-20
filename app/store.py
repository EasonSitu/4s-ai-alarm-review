import hashlib
import json
import re
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


def migrate_image_identity(c):
    """Replace the old UNIQUE constraint while retaining task IDs, children and indexes."""
    if "image_id" in {row["name"] for row in c.execute("PRAGMA table_info(tasks)")}:
        return
    ddl = c.execute("SELECT sql FROM sqlite_schema WHERE name='tasks'").fetchone()[0]
    # SQLite appends ALTER TABLE columns before the existing table constraint.
    # Reuse the installed schema to retain columns from earlier releases.
    table, n = re.subn(r"(?i)UNIQUE\s*\(\s*project_id\s*,\s*alert_id\s*\)",
                       "image_id TEXT NOT NULL DEFAULT '', UNIQUE(project_id,alert_id,image_id)", ddl)
    if n != 1:
        raise RuntimeError("无法识别旧版报警索引")
    table, n = re.subn(r'(?i)^CREATE TABLE "?tasks"?\s*\(', "CREATE TABLE tasks_v2 (", table)
    if n != 1:
        raise RuntimeError("无法识别旧版任务表")
    indexes = c.execute("SELECT sql FROM sqlite_schema WHERE tbl_name='tasks' AND type IN ('index','trigger') AND sql IS NOT NULL").fetchall()
    cols = ",".join('"' + row["name"].replace('"', '""') + '"' for row in c.execute("PRAGMA table_info(tasks)"))
    c.execute("DROP VIEW IF EXISTS pilot_tasks")
    c.execute(table)
    c.execute(f"INSERT INTO tasks_v2 ({cols}) SELECT {cols} FROM tasks")
    c.execute("DROP TABLE tasks")
    c.execute("ALTER TABLE tasks_v2 RENAME TO tasks")
    for row in indexes:
        c.execute(row[0])
    if c.execute("PRAGMA foreign_key_check").fetchone():
        raise RuntimeError("迁移后任务关联检查失败")


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
            callback_error TEXT, image_id TEXT NOT NULL DEFAULT '', UNIQUE(project_id,alert_id,image_id)
        );
        CREATE TABLE IF NOT EXISTS images (task_id TEXT PRIMARY KEY REFERENCES tasks, mime TEXT NOT NULL, data BLOB NOT NULL);
        CREATE INDEX IF NOT EXISTS review_queue ON tasks(status, review_due, received_at);
        CREATE INDEX IF NOT EXISTS delivery_queue ON tasks(callback_status, callback_due, received_at);
        CREATE INDEX IF NOT EXISTS received ON tasks(received_at);
        """)
        # SQLite requires foreign keys disabled outside the transaction for table rebuilds.
        c.execute("PRAGMA foreign_keys=OFF")
        c.execute("BEGIN IMMEDIATE")
        migrate_image_identity(c)
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
        c.execute("""CREATE TABLE IF NOT EXISTS task_frames(
            task_id TEXT NOT NULL REFERENCES tasks, idx INTEGER NOT NULL, mime TEXT, data BLOB,
            roi TEXT, person_boxes TEXT, status TEXT NOT NULL, verdict TEXT, reason TEXT, error TEXT, model TEXT,
            PRIMARY KEY(task_id,idx))""")
        # Old originals stay in the images table. Migrate only their metadata.
        c.execute("""INSERT INTO task_frames(task_id,idx,roi,person_boxes,status,verdict,reason,error,model)
                     SELECT task_id,1,roi,person_boxes,
                     CASE WHEN status IN ('completed','failed') THEN status ELSE 'pending' END,
                     verdict,reason,error,model FROM tasks t WHERE NOT EXISTS
                     (SELECT 1 FROM task_frames f WHERE f.task_id=t.task_id)""")
        history_cols = {r[1] for r in c.execute("PRAGMA table_info(model_call_history)")}
        if history_cols and "image_index" not in history_cols:
            c.execute("ALTER TABLE model_call_history RENAME TO old_model_calls")
        c.execute("""CREATE TABLE IF NOT EXISTS model_call_history(
            task_id TEXT NOT NULL REFERENCES tasks, attempt INTEGER NOT NULL, role TEXT NOT NULL,
            profile TEXT, endpoint_origin TEXT, requested_model TEXT, actual_model TEXT,
            started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, error TEXT, image_index INTEGER NOT NULL,
            PRIMARY KEY(task_id,attempt,image_index,role))""")
        if history_cols and "image_index" not in history_cols:
            c.execute("INSERT INTO model_call_history SELECT *,1 FROM old_model_calls")
            c.execute("DROP TABLE old_model_calls")
        c.execute("UPDATE review_history SET status='interrupted',finished_at=? WHERE status='processing'", (now(),))
        c.execute("UPDATE model_call_history SET status='interrupted',finished_at=? WHERE status='processing'", (now(),))
        interrupted = c.execute("""SELECT task_id,project_id FROM tasks WHERE historical_test=0 AND status='processing'
                                   AND review_attempts>=?""", (len(REVIEW_DELAYS)+1,)).fetchall()
        for row in interrupted:
            task_id = row["task_id"]
            callback = "pending" if callback_ready(cfg, mode, row["project_id"]) else "unconfigured"
            c.execute("""UPDATE task_frames SET status='failed',error=COALESCE(error,'最后一次审核中断')
                         WHERE task_id=? AND status!='completed'""", (task_id,))
            status, verdict, reason, error, model = aggregate(frame_results(c, task_id))
            c.execute("""UPDATE tasks SET status=?,verdict=?,reason=?,error=?,model=?,finished_at=?,callback_status=?
                         WHERE task_id=?""", (status, verdict, reason, error, model, now(), callback, task_id))
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
        scope = cfg.get("callback_test_project")
        route = [cfg["callback_url"], cfg["callback_token"]]
        if scope:
            route.append(scope)
            c.execute("""UPDATE tasks SET callback_status='unconfigured' WHERE historical_test=0
                         AND project_id<>? AND callback_status IN ('pending','retrying')""", (scope,))
        fingerprint = hashlib.sha256(json.dumps(route).encode()).hexdigest()
        previous = c.execute("SELECT fingerprint FROM callback_config WHERE id=1").fetchone()
        auto_backfill = callback_ready(cfg, mode) and cfg.get("callback_auto_backfill", True)
        if auto_backfill and (previous is None or previous[0] != fingerprint):
            c.execute("""UPDATE tasks SET callback_status='pending',callback_attempts=0,callback_due=0,
                         callback_error=NULL WHERE historical_test=0 AND project_id=COALESCE(?,project_id)
                         AND callback_status IN ('failed','retrying','pending','unconfigured')""",(scope,))
        c.execute("INSERT OR REPLACE INTO callback_config VALUES(1,?)", (fingerprint,))
        if auto_backfill:
            c.execute("UPDATE tasks SET callback_status='pending' WHERE historical_test=0 AND project_id=COALESCE(?,project_id) AND callback_status='unconfigured'", (scope,))
        elif not callback_ready(cfg, mode):
            c.execute("UPDATE tasks SET callback_status='unconfigured' WHERE historical_test=0 AND callback_status IN ('retrying','pending')")


class Conflict(Exception):
    pass


def add(mode, cfg, meta, data, mime, signature, extra_frames=()):
    with db(mode) as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM tasks WHERE project_id=? AND alert_id=? AND image_id=?", (meta["project_id"], meta["alert_id"], meta.get("image_id") or "")).fetchone()
        if row:
            if row["signature"] != signature:
                raise Conflict
            return view(mode, row, c), False
        if c.execute("SELECT 1 FROM tasks WHERE project_id=? AND alert_id=?",
                     (meta["project_id"], meta["alert_id"])).fetchone():
            raise Conflict
        task_id = uuid.uuid4().hex
        model = local_review_model(mode, meta["project_id"], meta["alarm_type"])
        external = model is None
        waiting = external and not model_ready(cfg)
        c.execute("""INSERT INTO tasks(task_id,project_id,alert_id,alarm_type,occurred_at,camera_id,roi,
                   signature,received_at,status,rule_version,person_boxes,image_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (task_id, meta["project_id"], meta["alert_id"], meta["alarm_type"], meta["occurred_at"],
                   meta["camera_id"], json.dumps(None if extra_frames else meta["roi"]), signature, now(), "awaiting_model" if waiting else "queued", RULE, json.dumps(None if extra_frames else meta.get("person_boxes")), meta.get("image_id") or ""))
        for index, (frame, raw, kind, _) in enumerate([(meta, data, mime, signature), *extra_frames], 1):
            c.execute("""INSERT INTO task_frames(task_id,idx,roi,person_boxes,mime,data,status)
                         VALUES(?,?,?,?,?,?,'pending')""",
                      (task_id,index,json.dumps(frame["roi"]),json.dumps(frame.get("person_boxes")),kind,raw))
        return view(mode, c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone(), c), True


def frame_results(c, task_id):
    rows = c.execute("""SELECT idx AS 'index',status,verdict,reason,error,model,roi,person_boxes FROM task_frames
                        WHERE task_id=? ORDER BY idx""", (task_id,))
    return [dict(dict(r), roi=json.loads(r["roi"] or "null"), person_boxes=json.loads(r["person_boxes"] or "null")) for r in rows]


def original(c, task_id, index):
    return c.execute("""SELECT COALESCE(f.mime,i.mime) AS mime, COALESCE(f.data,i.data) AS data
                        FROM task_frames f LEFT JOIN images i ON i.task_id=f.task_id AND f.idx=1
                        WHERE f.task_id=? AND f.idx=?""", (task_id,index)).fetchone()


def aggregate(frames):
    """Called only after every frame has a verdict or terminal error."""
    models = list(dict.fromkeys(f["model"] for f in frames if f["model"]))
    model = ", ".join(models) or None
    valid = next((f for f in frames if f["verdict"] == "valid"), None)
    if valid:
        reason = valid["reason"] if len(frames)==1 else f"第 {valid['index']} 张：{valid['reason']}"
        return "completed", "valid", reason, None, model
    if all(f["verdict"] == "false_alarm" for f in frames):
        reason = frames[0]["reason"] if len(frames)==1 else f"全部 {len(frames)} 张图片均可排除本条违规。"
        return "completed", "false_alarm", reason, None, model
    if all(f["status"] == "failed" for f in frames):
        return "failed", None, None, frames[0]["error"] if len(frames)==1 else "全部图片审核失败，详见逐图错误。", model
    reason = frames[0]["reason"] if len(frames)==1 else "未见明确违规，部分图片证据不足或审核失败。"
    return "completed", "uncertain", reason, None, model


def view(mode, row, c):
    data = dict(row)
    data["image_id"] = data["image_id"] or None
    data["person_boxes"] = json.loads(data["person_boxes"]) if data["person_boxes"] else None
    data.update(mode=mode, roi=json.loads(data["roi"]), images=frame_results(c, row["task_id"]))
    return Task.model_validate(data).model_dump()


def get(mode, task_id):
    with db(mode) as c:
        c.execute("BEGIN")
        row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return view(mode, row, c) if row else None
