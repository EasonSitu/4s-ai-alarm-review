"""Restart the fixed HTTPS listener and verify the same URL and durable task ID."""
import base64
import json
import os
import ssl
import subprocess
import tempfile
from pathlib import Path
from urllib import error, request

from pilot import ROOT, unused_port, until


def run(state):
    tls = state / "tls"
    tls.mkdir()
    cert = tls / "fullchain.pem"
    profile = tls / "cert.cnf"
    profile.write_text("[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=ext\n"
                       "[dn]\nCN=4S local test\n[ext]\nsubjectAltName=IP:127.0.0.1\n"
                       "basicConstraints=critical,CA:FALSE\nextendedKeyUsage=serverAuth\n")
    subprocess.run(["openssl", "req", "-new", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-days", "1", "-config", str(profile), "-keyout", str(tls / "privkey.pem"),
                    "-out", str(cert)], check=True, capture_output=True)

    port, https_port = unused_port(), unused_port()
    while https_port == port:
        https_port = unused_port()
    env = dict(os.environ, FOUR_S_STATE=str(state), FOUR_S_BIND="127.0.0.1",
               FOUR_S_HTTPS_BIND="127.0.0.1", FOUR_S_HTTPS_PORT=str(https_port))
    def manage(*args, check=True):
        return subprocess.run([str(ROOT / "4s"), *args], env=env, capture_output=True,
                              text=True, timeout=25, check=check)
    config = state / "config.json"
    manage("model-list")
    settings = dict(json.loads(config.read_text()), port=port)
    config.write_text(json.dumps(settings))
    base = f"https://127.0.0.1:{https_port}"
    client = request.build_opener(request.ProxyHandler({}), request.HTTPSHandler(
        context=ssl.create_default_context(cafile=str(cert))))
    def call(path, body=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = request.Request(base + path, data=json.dumps(body).encode() if body else None, headers=headers)
        try:
            with client.open(req, timeout=4) as response:
                return response.status, json.load(response)
        except error.HTTPError as exc:
            return exc.code, None

    photo = base64.b64encode((ROOT / "examples/test-card.png").read_bytes()).decode()
    payload = dict(project_id="fixed-qa", alert_id="probe", alarm_type="no_helmet", image_base64=photo)
    try:
        manage("fixed-start", "127.0.0.1")
        assert call("/healthz") == (200, {"status": "ok"})
        assert call("/admin")[0] == 404
        assert call("/sandbox/api/v1/tasks", payload, "obsolete-token")[0] == 404
        assert call("/sandbox/api/v1/tasks/"+"a"*32, token="obsolete-token")[0] == 404
        assert call("/api/v1/tasks", payload)[0] == 401
        status, task = call("/api/v1/tasks", payload, settings["live_token"])
        assert status == 202 and task["status"] == "awaiting_model" and "should_filter" not in task
        assert task["verdict"] is None and task["callback_status"] == "waiting_result"
        assert manage("fixed-start", "127.0.0.2", check=False).returncode != 0
        assert (state / "fixed-ip.txt").read_text().strip() == "127.0.0.1"
        manage("stop")
        (state / "public-admin-origin.txt").write_text(base)
        (state / "admin.secret").write_text("fixture-123")
        manage("fixed-start")
        assert call("/healthz") == (200, {"status": "ok"})
        status, repeat = call("/api/v1/tasks", payload, settings["live_token"])
        assert status == 200 and repeat["task_id"] == task["task_id"]
        admin_path = "/admin/api/live/tasks"
        for path in ("/admin", admin_path, admin_path+"/"+task["task_id"]+"/image"):
            assert call(path, token=settings["live_token"])[0] == 401
        password = (state/"admin.secret").read_text().strip()
        basic = "Basic "+base64.b64encode(("admin:"+password).encode()).decode()
        auth = {"Authorization":basic}
        with client.open(request.Request(base+admin_path+"?project=absent", headers=auth)) as response:
            assert json.load(response)["total"] == 0
        with client.open(request.Request(base+admin_path+"/"+task["task_id"]+"/image?index=1", headers=auth)) as response:
            assert response.read() == base64.b64decode(photo)
        for origin, expected in ((base,202), ("https://other.example",403)):
            headers = {**auth, "Origin":origin, "X-Admin-Action":"1","Content-Type":"application/json"}
            req = request.Request(base+admin_path, data=json.dumps(dict(payload, alert_id="admin-check")).encode(), headers=headers)
            try:
                with client.open(req) as response:
                    code = response.status
            except error.HTTPError as exc:
                code = exc.code
            assert code == expected
        for _ in range(8):
            assert call("/admin", token="wrong")[0] == 401
        assert call("/admin", token="wrong")[0] == 429
        assert call("/healthz") == (200, {"status":"ok"})
        manage("stop")
        manage("rotate-token", "live")
        old_token = settings["live_token"]
        settings = json.loads(config.read_text())
        assert old_token != settings["live_token"]
        manage("fixed-start")
        assert call("/api/v1/tasks", payload, old_token)[0] == 401
        assert call("/api/v1/tasks", payload, settings["live_token"])[0] == 200
        status, rule = call("/api/v1/tasks", dict(payload, alert_id="scope", alarm_type="danger_zone"),
                            settings["live_token"])
        assert status == 202
        lookup = "/api/v1/tasks/" + rule["task_id"]
        until(lambda: call(lookup, token=settings["live_token"])[1]["verdict"] == "uncertain")
        print("PASS verified TLS; restart at identical URL; idempotent replay; PPE hold; scope rule; admin isolation")
    finally:
        manage("stop")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(dir=ROOT / "var", prefix="fixed-qa-") as folder:
        run(Path(folder))
