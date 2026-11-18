"""Exercise local model settings and live switching using two isolated model servers."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pilot import Pilot, ROOT, until


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        server = self.server
        server.calls.append((data["model"], self.headers["Authorization"]))
        if server.block:
            server.entered.set()
            server.release.wait(12)
        evidence = {
            "relevant_person_visible": True,
            "is_site_image": True,
            "saw_target_violation": True,
            "can_rule_out_every_target_violation": False,
            "reason": "本机测试替身返回的证据。",
        }
        result = {"model": data["model"], "choices": [{"message": {"content": json.dumps(evidence)}}]}
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def provider():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    server.calls = []
    server.block = False
    server.entered, server.release = threading.Event(), threading.Event()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run(folder):
    first, second = provider(), provider()
    site = Pilot(folder, None)
    bootstrap = """
import os
import uvicorn
import app.main as main
from app.review import RemoteError
def probe(candidate):
    if candidate["model_key"] == "rejected-fixture":
        raise RemoteError("测试密钥无效")
    return {"image_readback": True}
main.probe = probe
uvicorn.run(main.api, host="127.0.0.1", port=int(os.environ["MODEL_TEST_PORT"]),
            access_log=False, log_level="error")
"""
    site.env["MODEL_TEST_PORT"] = str(site.port)

    def start(initial=False):
        if initial:
            (site.state / "config.json").write_text(json.dumps(site.cfg))
        site.process = subprocess.Popen([sys.executable, "-B", "-c", bootstrap], env=site.env,
                                        stdout=site.log, stderr=site.log, cwd=ROOT)
        def ready():
            if site.process.poll() is not None:
                raise AssertionError((site.state / "qa.log").read_text()[-2000:])
            try:
                return site.api("/healthz")[0] == 200
            except OSError:
                return False
        until(ready)

    def admin(path, payload):
        return site.api("/admin/api/models/" + path, payload, **{"X-Admin-Action": "1"})

    def settings(name, server, key):
        return {"name": name, "model_base": f"http://127.0.0.1:{server.server_port}/v1",
                "model": name + "-vision", "model_key": key}

    def select(name):
        profiles = site.api("/admin/api/models")[1]["profiles"]
        revision = next(profile["revision"] for profile in profiles if profile["name"] == name)
        code, result = admin("activate", {"name": name, "revision": revision})
        assert code == 200, (code, result)

    try:
        start(initial=True)
        a = settings("first", first, "first-fixture-key")
        b = settings("second", second, "second-fixture-key")
        assert admin("save", a)[0] == 200 and admin("save", b)[0] == 200
        listed = site.api("/admin/api/models")[1]
        assert listed["active_profile"] == "off" and len(listed["profiles"]) == 2
        assert "fixture-key" not in json.dumps(listed)

        select("first")
        first.block = True
        first_task = site.submit("in-flight")
        assert first.entered.wait(5)
        select("second")
        second_task = site.submit("after-switch")
        first.release.set()
        assert site.wait(first_task, "status", "completed")["model"] == "first-vision"
        assert site.wait(second_task, "status", "completed")["model"] == "second-vision"
        assert first.calls == [("first-vision", "Bearer first-fixture-key")]
        assert second.calls[-1] == ("second-vision", "Bearer second-fixture-key")
        with sqlite3.connect(site.state / "live/tasks.sqlite3") as db:
            for task, profile, server in ((first_task, "first", first), (second_task, "second", second)):
                entry = db.execute("SELECT profile,endpoint_origin FROM review_history WHERE task_id=?", (task,)).fetchone()
                assert entry == (profile, f"http://127.0.0.1:{server.server_port}")

        assert admin("save", dict(b, model_key="rotated-fixture-key"))[0] == 200
        staged = site.submit("staged-key")
        site.wait(staged, "status", "completed")
        assert second.calls[-1][1] == "Bearer second-fixture-key"
        select("second")
        rotated = site.submit("rotated-key")
        site.wait(rotated, "status", "completed")
        assert second.calls[-1][1] == "Bearer rotated-fixture-key"

        assert admin("save", dict(b, model_key="rejected-fixture"))[0] == 200
        revision = next(p["revision"] for p in site.api("/admin/api/models")[1]["profiles"] if p["name"] == "second")
        assert admin("activate", {"name": "second", "revision": revision})[0] == 400
        unchanged = site.submit("failed-switch")
        site.wait(unchanged, "status", "completed")
        assert second.calls[-1][1] == "Bearer rotated-fixture-key"
        assert admin("save", dict(b, model_base="https://elsewhere.invalid/v1", model_key=""))[0] == 422
        assert admin("save", dict(b, model_key="rotated-fixture-key"))[0] == 200
        assert admin("activate", {"name": "second", "revision": revision})[0] == 409

        assert admin("pause", {})[0] == 200
        held = site.submit("held")
        assert site.wait(held, "status", "awaiting_model")["verdict"] is None
        select("second")
        assert site.wait(held, "status", "completed")["model"] == "second-vision"
        site.down()
        start()
        current = site.api("/admin/api/models")[1]
        assert current["active_profile"] == "second" and current["active_model"] == "second-vision"
        assert "fixture-key" not in json.dumps(current)
        assert oct((site.state / "config.json").stat().st_mode & 0o777) == "0o600"
        print("PASS save and rotate keys, switching without restart, in-flight snapshot, failed probe, pause and recovery")
    finally:
        first.release.set()
        site.down()
        site.log.close()
        for server in (first, second):
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(dir=ROOT / "var", prefix="admin-model-qa-") as folder:
        run(Path(folder))
