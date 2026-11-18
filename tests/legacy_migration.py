"""Verify old fixed-result records remain retrievable and never become production work."""
import base64
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(folder):
    os.environ["FOUR_S_STATE"] = str(folder)
    from app.config import init_config
    from app.contracts import Submission, unpack
    from app import store
    from app.worker import claim

    config = init_config()
    (folder / "live").mkdir()
    image = (ROOT / "examples/test-card.png").read_bytes()
    payload = {"project_id": "test-4s", "alert_id": "old-record", "alarm_type": "no_helmet",
               "image_base64": base64.b64encode(image).decode()}
    meta, data, mime, signature = unpack(Submission.model_validate(payload))
    with sqlite3.connect(folder / "live/tasks.sqlite3") as c:
        c.executescript("""
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, alert_id TEXT NOT NULL,
            alarm_type TEXT NOT NULL, occurred_at TEXT, camera_id TEXT, roi TEXT,
            signature TEXT NOT NULL, received_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
            status TEXT NOT NULL, verdict TEXT, reason TEXT, error TEXT, model TEXT, elapsed_ms INTEGER,
            rule_version TEXT NOT NULL, review_attempts INTEGER NOT NULL DEFAULT 0,
            review_due REAL NOT NULL DEFAULT 0, callback_status TEXT NOT NULL DEFAULT 'waiting_result',
            callback_attempts INTEGER NOT NULL DEFAULT 0, callback_due REAL NOT NULL DEFAULT 0,
            callback_error TEXT, person_boxes TEXT, UNIQUE(project_id,alert_id)
        );
        CREATE TABLE images (task_id TEXT PRIMARY KEY REFERENCES tasks, mime TEXT NOT NULL, data BLOB NOT NULL);
        """)
        c.execute("""INSERT INTO tasks(task_id,project_id,alert_id,alarm_type,roi,signature,received_at,status,verdict,model,rule_version,callback_status)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                  ("a"*32, meta["project_id"], meta["alert_id"], meta["alarm_type"], "null", signature,
                   "2026-01-01T00:00:00Z", "completed", "uncertain", "test-fixed", "ppe-2", "pending"))
        c.execute("INSERT INTO images VALUES(?,?,?)", ("a"*32, mime, data))
        c.execute("""INSERT INTO tasks(task_id,project_id,alert_id,alarm_type,roi,signature,received_at,status,verdict,model,rule_version,callback_status)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                  ("b"*32, "vision-test-4s", "actual-model", "no_helmet", "null", "separate-signature",
                   "2026-01-01T00:00:01Z", "completed", "uncertain", "deepseek-flash", "ppe-2", "unconfigured"))
        c.execute("INSERT INTO images VALUES(?,?,?)", ("b"*32, mime, data))

    store.init("live", config)
    old = store.get("live", "a"*32)
    assert old["model"] == "test-fixed" and old["verdict"] == "uncertain"
    replay, fresh = store.add("live", config, meta, data, mime, signature)
    assert not fresh and replay["task_id"] == old["task_id"]
    fresh_meta, fresh_data, fresh_mime, fresh_sig = unpack(Submission.model_validate(dict(payload, alert_id="new-record")))
    new, fresh = store.add("live", config, fresh_meta, fresh_data, fresh_mime, fresh_sig)
    assert fresh and new["status"] == "awaiting_model" and new["verdict"] is None
    config.update(model="fixture", model_base="https://example.invalid", model_key="fixture",
                  callback_url="https://callback.invalid", callback_token="fixture")
    store.init("live", config)
    with store.db("live") as c:
        assert c.execute("SELECT historical_test FROM tasks WHERE task_id=?", (old["task_id"],)).fetchone()[0] == 1
        assert c.execute("SELECT data FROM images WHERE task_id=?", (old["task_id"],)).fetchone()[0] == image
        assert {row[0] for row in c.execute("SELECT task_id FROM pilot_tasks")} == {new["task_id"]}
        assert c.execute("SELECT historical_test FROM tasks WHERE task_id=?", ("b"*32,)).fetchone()[0] == 1
        assert c.execute("SELECT callback_status FROM tasks WHERE task_id=?", ("b"*32,)).fetchone()[0] == "unconfigured"
        c.execute("UPDATE tasks SET status='queued' WHERE task_id=?", (old["task_id"],))
    from app.main import admin_list, cfg
    cfg.update(config)
    visible = admin_list("live", page=1)
    assert {task["task_id"] for task in visible["items"]} == {new["task_id"], "b"*32}
    assert claim("live", False, config)["task_id"] == new["task_id"]
    assert claim("live", True, config) is None
    print("PASS old records retained and excluded; new tasks with any project ID use the production queue")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(dir=ROOT / "var", prefix="legacy-qa-") as tmp:
        run(Path(tmp))
