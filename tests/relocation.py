"""Rehearse a cold restore between isolated application directories on the data volume."""
import hashlib
import json
import secrets
import shutil
import sqlite3
import tempfile
from pathlib import Path

from pilot import Pilot, ROOT


def project(target):
    for name in ("app", "scripts", "web", "examples"):
        shutil.copytree(ROOT / name, target / name, ignore=shutil.ignore_patterns("__pycache__"))
    (target / "var").mkdir()
    return target / "var"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(scratch):
    old, new = scratch / "old-site", scratch / "relocated-site"
    source = Pilot(project(old), None, old)
    dest = None
    try:
        source.cfg["live_token"] = secrets.token_urlsafe(32)
        source.up()
        waiting = source.submit("waiting-at-cutover")
        completed = source.submit("saved-at-cutover", "danger_zone")
        original = source.wait(completed, "status", "completed")
        assert original["verdict"] == "uncertain"
        image = source.api("/admin/api/live/tasks/" + completed + "/image")[1]
        source.down()
        target_state = project(new)
        shutil.copytree(source.state / "live", target_state / "live")
        shutil.copy2(source.state / "config.json", target_state / "config.json")
        paths = ["config.json"] + [str(p.relative_to(source.state)) for p in (source.state / "live").iterdir() if p.is_file()]
        assert all(digest(target_state / name) == digest(source.state / name) for name in paths)
        dest = Pilot(target_state, None, new)
        dest.cfg = json.loads((target_state / "config.json").read_text())
        dest.cfg["port"] = dest.port
        dest.up()
        assert dest.api("/api/v1/tasks/" + completed)[1] == original
        assert dest.api("/admin/api/live/tasks/" + completed + "/image")[1] == image
        assert dest.wait(waiting, "status", "awaiting_model")["review_attempts"] == 0
        status, duplicate = dest.api("/api/v1/tasks", dest.payload("waiting-at-cutover"))
        assert status == 200 and duplicate["task_id"] == waiting
        with sqlite3.connect(target_state / "live/tasks.sqlite3") as db:
            assert db.execute("pragma integrity_check").fetchone()[0] == "ok"
            assert not db.execute("pragma foreign_key_check").fetchall()
        print("PASS cold restore of tasks, original image, pending work and idempotent replay")
    finally:
        for site in (dest, source):
            if site:
                site.down()
                site.log.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(dir=ROOT / "var", prefix="relocation-qa-") as folder:
        run(Path(folder))
