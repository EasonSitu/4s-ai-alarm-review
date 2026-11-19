"""Exercise primary/fallback routing, timeout and model discovery through HTTP."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from pilot import Pilot, ROOT, until


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def answer(self, code, data):
        body = json.dumps(data).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        self.server.catalog_keys.append(self.headers.get("Authorization"))
        if self.path.startswith("/api/v1/models?"):
            query = parse_qs(urlsplit(self.path).query)
            assert query.get("capabilities") == ["VU"]
            page = int(query["page_no"][0])
            rows = ([{"model": "bailian-one", "inference_metadata": {"request_modality": ["Image", "Text"]}},
                     {"model": "text-only", "inference_metadata": {"request_modality": ["Text"]}}] if page == 1 else
                    [{"model": "bailian-two", "inference_metadata": {"request_modality": ["Image"]}}])
            return self.answer(200, {"output": {"models": rows, "total": 3}})
        self.answer(200, {"data": [
            {"id": "vision-one", "input_modalities": ["text", "image"]},
            {"id": "text-only", "input_modalities": ["text"]},
            {"id": "unknown-capabilities"},
        ]})

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        server = self.server
        server.calls.append((data["model"], self.headers.get("Authorization")))
        behavior = server.behavior
        if behavior == "slow":
            time.sleep(2.2)
        if behavior == "error":
            return self.answer(503, {})
        if behavior == "bad-json":
            return self.answer(200, {"choices": [{"message": {"content": "invalid"}}]})
        evidence = {"relevant_person_visible": True, "is_site_image": True,
                    "saw_target_violation": behavior != "uncertain",
                    "can_rule_out_every_target_violation": False, "reason": "本机替身的审核证据。"}
        self.answer(200, {"model": data["model"], "choices": [{"message": {"content": json.dumps(evidence)}}]})


def provider():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    server.daemon_threads = True
    server.calls, server.catalog_keys, server.behavior = [], [], "valid"
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
    if candidate['model_key'] == 'rejected-fixture':
        raise RemoteError('测试密钥无效')
    return {'image_readback': True}
main.probe = probe
uvicorn.run(main.api, host='127.0.0.1', port=int(os.environ['MODEL_TEST_PORT']), access_log=False, log_level='error')
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

    def save(name, server, key):
        form = {"name": name, "provider": "compatible", "model": name + "-vision",
                "model_base": f"http://127.0.0.1:{server.server_port}/v1", "model_key": key}
        assert admin("save", form)[0] == 200
        return form

    def route():
        listed = site.api("/admin/api/models")[1]
        by_name = {p["name"]: p for p in listed["profiles"]}
        return {"primary": "first", "primary_revision": by_name["first"]["revision"],
                "fallback": "second", "fallback_revision": by_name["second"]["revision"], "timeout_seconds": 1}

    def task(task_id):
        return site.api("/api/v1/tasks/" + task_id)[1]

    def history(task_id):
        return site.api("/admin/api/live/tasks/" + task_id)[1]["model_calls"]

    try:
        start(initial=True)
        a = save("first", first, "first-fixture-key")
        b = save("second", second, "second-fixture-key")
        code, catalog = admin("catalog", {"provider": "compatible", "name": "first", "model_base": a["model_base"]})
        assert code == 200 and [m["id"] for m in catalog["models"]] == ["vision-one", "unknown-capabilities"]
        assert first.catalog_keys == ["Bearer first-fixture-key"]
        assert "first-fixture-key" not in json.dumps(catalog)
        assert admin("catalog", {"provider": "compatible", "name": "first", "model_base": b["model_base"]})[0] == 400
        code, catalog = admin("catalog", {"provider": "bailian", "name": "second", "model_base": b["model_base"]})
        assert code == 200 and [m["id"] for m in catalog["models"]] == ["bailian-one", "bailian-two"]
        assert second.catalog_keys == ["Bearer second-fixture-key"] * 2
        assert admin("routing", dict(route(), fallback="first"))[0] == 422
        assert admin("routing", route())[0] == 200

        first.behavior = "slow"
        start_time = time.monotonic()
        slow = site.submit("primary-timeout")
        result = until(lambda: task(slow) if task(slow)["status"] == "completed" else None, timeout=8)
        assert result["model"] == "second-vision" and result["verdict"] == "valid"
        assert 0.8 <= time.monotonic() - start_time < 2.1
        calls = history(slow)
        assert [(c["role"], c["status"]) for c in calls] == [("primary", "failed"), ("fallback", "completed")]
        assert "fixture-key" not in json.dumps(calls)
        time.sleep(1.5)
        assert task(slow)["model"] == "second-vision" and len(second.calls) == 1

        first.behavior = "error"
        error_task = site.submit("primary-error")
        assert site.wait(error_task, "status", "completed")["model"] == "second-vision"
        first.behavior = "uncertain"
        count = len(second.calls)
        unknown = site.submit("primary-uncertain")
        assert site.wait(unknown, "status", "completed")["verdict"] == "uncertain"
        assert len(second.calls) == count and len(history(unknown)) == 1
        print("PASS timeout and technical-error fallback; uncertain remains a primary result", flush=True)

        first.behavior = second.behavior = "error"
        failed = site.submit("both-fail")
        for attempt in (1, 2):
            until(lambda: site.attempt(failed, "status", "retrying", "review_attempts", attempt))
            with sqlite3.connect(site.state / "live/tasks.sqlite3") as db:
                db.execute("UPDATE tasks SET review_due=0 WHERE task_id=?", (failed,))
        result = site.wait(failed, "status", "failed")
        assert result["verdict"] is None and len(history(failed)) == 6
        print("PASS both failures retry and end with no business verdict", flush=True)

        second.behavior = "valid"
        save("second", second, "rotated-fixture-key")
        site.down(); start()
        restarted = site.api("/admin/api/models")[1]
        assert restarted["fallback_profile"] == "second" and restarted["timeout_seconds"] == 1
        stable = site.submit("snapshot-after-restart")
        assert site.wait(stable, "status", "completed")["model"] == "second-vision"
        assert second.calls[-1][1] == "Bearer second-fixture-key"
        save("second", second, "rejected-fixture")
        assert admin("routing", route())[0] == 400
        assert site.api("/admin/api/models")[1]["fallback_model"] == "second-vision"
        assert admin("pause", {})[0] == 200
        held = site.submit("pause-holds")
        assert site.wait(held, "status", "awaiting_model")["verdict"] is None
        print("PASS restart, immutable active credentials, failed verification and pause", flush=True)
    finally:
        site.down(); site.log.close()
        for server in (first, second):
            server.shutdown(); server.server_close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(dir=ROOT / "var", prefix="fallback-qa-") as folder:
        run(Path(folder))
