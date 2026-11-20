"""End-to-end pilot checks. Uses only generated images, isolated Data storage and loopback peers."""
import base64
import concurrent.futures
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error, request

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
LOCAL = request.build_opener(request.ProxyHandler({}))


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def until(check, timeout=15):
    deadline = time.monotonic()+timeout
    while time.monotonic()<deadline:
        value = check()
        if value:
            return value
        time.sleep(.06)
    raise AssertionError("timed out: "+str(check))


class Peer(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        peer = self.server
        body = json.load(io.BytesIO(self.rfile.read(int(self.headers["Content-Length"]))))
        if self.path == "/v1/chat/completions":
            assert self.headers.get("Authorization")=="Bearer qa-model"
            assert body["model"]=="qwen3.7-plus"
            url = body["messages"][0]["content"][0]["image_url"]["url"]
            raw = base64.b64decode(url.split(",")[1], validate=True)
            with Image.open(io.BytesIO(raw)) as img:
                peer.size = img.size
            peer.model_calls+=1
            if peer.block_review:
                peer.review_enter.set()
                peer.review_release.wait(10)
            if peer.model_response=="http-error":
                return self.reply(503,{})
            content = "invalid-json" if peer.model_response=="invalid" else json.dumps({
                "relevant_person_visible":peer.model_response!="empty",
                "is_site_image":peer.model_response!="card",
                "saw_target_violation":peer.model_response=="valid",
                "can_rule_out_every_target_violation":peer.model_response in ("false_alarm","empty","card"),
                "reason":"本地接口替身的测试证据。"})
            return self.reply(200,{"model":"test-provider-only", "choices":[{"message":{"content":content}}]})
        assert self.path == "/callback"
        assert self.headers.get("Authorization")==getattr(peer, "callback_auth", "Bearer qa-callback")
        peer.events.append(body)
        if peer.block_delivery:
            peer.delivery_enter.set()
            peer.delivery_release.wait(10)
        self.reply(peer.callback_code,{})

    def reply(self, code, data):
        try:
            self.send_response(code)
            self.send_header("Content-Type","application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        except (BrokenPipeError, ConnectionResetError):
            pass


class Pilot:
    def __init__(self, state, peer, app_root=ROOT):
        self.state, self.peer, self.app_root = state, peer, app_root
        self.port=unused_port()
        self.base=f"http://127.0.0.1:{self.port}"
        self.env=dict(os.environ, FOUR_S_STATE=str(state), FOUR_S_BIND="127.0.0.1", TMPDIR=str(state))
        for key in ("DASHSCOPE_API_KEY","BAILIAN_API_KEY","DASHSCOPE_BASE_URL"):
            self.env.pop(key,None)
        self.cfg=dict(port=self.port, live_token="L"*32, mock_token="M"*32, receipt_token="R"*32,
                      model="qwen3.7-plus",model_base="",model_key="",callback_url="",callback_token="")
        self.process=None
        self.log=(state/"qa.log").open("ab")
        self.sample=base64.b64encode((ROOT/"examples/test-card.png").read_bytes()).decode()

    def api(self, path, payload=None, token="live_token", **headers):
        data=None if payload is None else json.dumps(payload).encode()
        auth = "Bearer "+self.cfg[token]
        if path.startswith("/admin"):
            password = (self.state/"admin.secret").read_text().strip()
            auth = "Basic "+base64.b64encode(("admin:"+password).encode()).decode()
        req=request.Request(self.base+path, data=data, headers={"Authorization":auth,
                            "Content-Type":"application/json", **headers})
        try:
            with LOCAL.open(req,timeout=4) as response:
                data=response.read()
                return response.status, json.loads(data) if "application/json" in response.headers.get("Content-Type","") else data
        except error.HTTPError as exc:
            return exc.code, exc.read()

    def up(self):
        (self.state/"config.json").write_text(json.dumps(self.cfg))
        self.process=subprocess.Popen([sys.executable,"-B",str(self.app_root/"scripts/serve.py")],env=self.env,
                                      stdout=self.log,stderr=self.log,cwd=self.app_root)
        def ready():
            if self.process.poll() is not None:
                raise AssertionError((self.state/"qa.log").read_text()[-2000:])
            try:
                return self.api("/healthz")[0]==200
            except OSError:
                return False
        until(ready)

    def down(self, hard=False):
        if self.process and self.process.poll() is None:
            if hard:
                self.process.kill()
            else:
                self.process.terminate()
            self.process.wait(timeout=12)

    def payload(self, alert, kind="no_helmet"):
        return dict(project_id="qa",alert_id=alert,alarm_type=kind,image_base64=self.sample,
                    roi=[.25,0,.75,1],occurred_at="2026-11-17T09:00:00+08:00")

    def submit(self, alert, kind="no_helmet"):
        status,task=self.api("/api/v1/tasks",self.payload(alert,kind))
        assert status==202,(status,task)
        return task["task_id"]

    def wait(self,task,field,value):
        def read():
            code,t=self.api("/api/v1/tasks/"+task)
            assert code==200
            return t if t[field]==value else None
        return until(read)

    def advance(self, task, field):
        # Skip elapsed time only in the test database after verifying the persisted backoff.
        with sqlite3.connect(self.state/"live/tasks.sqlite3") as c:
            due=c.execute(f"SELECT {field} FROM tasks WHERE task_id=?",(task,)).fetchone()[0]
            assert due>time.time()
            c.execute(f"UPDATE tasks SET {field}=0 WHERE task_id=?",(task,))

    def run(self):
        self.up()
        q=self.payload("A")
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            responses=list(pool.map(lambda _:self.api("/api/v1/tasks",q),range(5)))
        assert sorted(code for code,_ in responses)==[200]*4+[202]
        assert len({t["task_id"] for _,t in responses})==1
        first=responses[0][1]["task_id"]
        assert responses[0][1]["status"]=="awaiting_model"
        q_test = dict(self.payload("previous-prefix"), project_id="test-4s", person_boxes=[[0,0,1,1]])
        code, sample = self.api("/api/v1/tasks", q_test)
        sample_id = sample["task_id"]
        assert code == 202 and sample["status"] == "awaiting_model" and sample["verdict"] is None
        assert sample["person_boxes"] == [[0,0,1,1]]
        assert self.api("/api/v1/tasks", dict(q_test,person_boxes=[]))[0] == 409
        assert self.api("/api/v1/tasks", dict(q_test,person_boxes=[[1,0,0,1]]))[0] == 422
        assert "/sandbox/api/v1/tasks" not in self.api("/openapi.json")[1]["paths"]
        assert self.api("/api/v1/tasks",dict(q,alarm_type="no_vest"))[0]==409
        assert self.api("/api/v1/tasks",q,token="mock_token")[0]==401
        assert self.api("/sandbox/api/v1/tasks/"+first,token="mock_token")[0]==404
        assert self.api("/api/v1/tasks",dict(q,image_base64="YWJj"))[0]==422
        assert self.api("/api/v1/tasks",dict(q,roi=[1,0,0,1]))[0]==422
        assert self.api("/api/v1/tasks",dict(q,occurred_at="2026-11-17T10:00:00"))[0]==422
        assert self.api("/api/v1/tasks",q,**{"Content-Type":"text/plain"})[0]==415
        assert self.api("/admin/api/live/tasks",q)[0]==403
        assert self.api("/admin",Host="site.invalid")[0]==403
        assert self.api("/admin/api/live/tasks/"+first+"/image")[1]==base64.b64decode(self.sample)
        for kind in ("combined","danger_zone"):
            t=self.wait(self.submit(kind,kind),"status","completed")
            assert (t["verdict"],t["callback_status"])==("uncertain","unconfigured")
        print("PASS durable intake, concurrent idempotency, validation, separation, fail-open scope",flush=True)

        p=self.peer
        peer_url=f"http://127.0.0.1:{p.server_address[1]}"
        self.down()
        self.cfg.update(model_base=peer_url+"/v1",model_key="qa-model",callback_url=peer_url+"/callback",callback_token="qa-callback")
        p.model_response="false_alarm";p.block_review=True
        self.up()
        assert p.review_enter.wait(5)
        self.wait(first,"status","processing")
        self.down(hard=True)
        p.block_review=False;p.review_release.set()
        self.up()
        t=self.wait(first,"callback_status","delivered")
        assert t["review_attempts"]==2 and t["verdict"]=="false_alarm" and "should_filter" not in t and p.size==(480,540)
        assert t["occurred_at"]=="2026-11-17T01:00:00Z"
        self.wait_by_alert_callback("combined")
        print("PASS pending model activation, ROI request, interrupted recognition recovery, saved callback backlog",flush=True)

        sample = self.wait(sample_id, "callback_status", "delivered")
        assert sample["model"] == "test-provider-only" and sample["verdict"] == "false_alarm"
        p.model_response = "valid"
        calls_before = p.model_calls
        for project in ("test-4s", "vision-test-4s", "site-01"):
            code, submitted = self.api("/api/v1/tasks", dict(q_test,project_id=project,alert_id="new-"+project))
            assert code == 202 and submitted["status"] == "queued" and submitted["verdict"] is None
            result = self.wait(submitted["task_id"], "callback_status", "delivered")
            assert result["model"] == "test-provider-only" and result["verdict"] == "valid"
            event = next(e for e in p.events if e["task_id"] == submitted["task_id"])
            assert event["received_at"] == result["received_at"] and "should_filter" not in event
        assert p.model_calls >= calls_before + 3
        with sqlite3.connect(self.state/"live/tasks.sqlite3") as c:
            assert c.execute("SELECT count(*) FROM pilot_tasks WHERE project_id IN ('test-4s','vision-test-4s')").fetchone()[0] >= 3
        print("PASS all project IDs use the same real model path and callbacks",flush=True)
        assert "should_filter" not in self.wait(first,"status","completed")
        for answer,verdict in (("empty","uncertain"),("card","uncertain"),("false_alarm","false_alarm")):
            p.model_response=answer
            result=self.wait(self.submit("evidence-"+answer),"callback_status","delivered")
            assert (result["verdict"],result["rule_version"])==(verdict,"ppe-3")
        assert self.api("/admin",Authorization="")[0]==401
        assert self.api("/admin",**{"X-Forwarded-For":"192.0.2.1"})[0]==403
        with sqlite3.connect(self.state/"live/tasks.sqlite3") as c:
            assert c.execute("SELECT status FROM review_history WHERE task_id=? ORDER BY attempt",(first,)).fetchall()==[("interrupted",),("completed",)]
        print("PASS verdict without filtering policy, empty & non-site evidence, admin gate, per-attempt history",flush=True)

        p.model_response="valid";p.callback_code=503
        task=self.submit("callback-retry","no_vest")
        t=self.wait(task,"callback_status","retrying")
        assert t["status"]=="completed" and t["verdict"]=="valid" and "should_filter" not in t
        calls=p.model_calls
        p.callback_code=200
        self.advance(task,"callback_due")
        t=self.wait(task,"callback_status","delivered")
        assert t["callback_attempts"]==2 and p.model_calls==calls
        sent=[e for e in p.events if e["task_id"]==task]
        assert len(sent)==2 and sent[0]==sent[1] and "image_base64" not in sent[0]

        p.model_response="http-error"
        task=self.submit("model-retry")
        t=self.wait(task,"status","retrying")
        assert t["verdict"] is None and "should_filter" not in t
        p.model_response="uncertain"
        self.advance(task,"review_due")
        t=self.wait(task,"callback_status","delivered")
        assert t["verdict"]=="uncertain" and t["review_attempts"]==2 and t["error"] is None

        p.model_response="invalid"
        task=self.submit("broken-model")
        for attempt in (1,2):
            t=until(lambda:self.attempt(task,"status","retrying","review_attempts",attempt))
            self.advance(task,"review_due")
        t=self.wait(task,"callback_status","delivered")
        assert t["review_attempts"]==3 and t["status"]=="failed" and t["verdict"] is None and "should_filter" not in t and t["error"]

        p.callback_code=503
        task=self.submit("exhaust-callback","combined")
        for attempt in range(1,5):
            until(lambda:self.attempt(task,"callback_status","retrying","callback_attempts",attempt))
            self.advance(task,"callback_due")
        t=self.wait(task,"callback_status","failed")
        assert t["callback_attempts"]==5 and t["status"]=="completed"
        p.callback_code=200
        assert self.api("/admin/api/live/tasks/"+task+"/resend",{},**{"X-Admin-Action":"1"})[0]==200
        assert self.wait(task,"callback_status","delivered")["review_attempts"]==1
        print("PASS retries of both stages, exhausted failures, null result, stable callback, manual resend",flush=True)

        p.block_delivery=True
        task=self.submit("kill-callback","danger_zone")
        assert p.delivery_enter.wait(5)
        self.down(hard=True)
        p.block_delivery=False;p.delivery_release.set()
        self.up()
        t=self.wait(task,"callback_status","delivered")
        assert t["review_attempts"]==1 and t["callback_attempts"]==2
        sent=[e for e in p.events if e["task_id"]==task]
        assert len(sent)==2 and sent[0]==sent[1]
        print("PASS interrupted HTTP callback recovery and duplicate-safe replay",flush=True)

        self.down()
        self.cfg.update(callback_test_project="callback-only", callback_token="")
        p.callback_auth=None
        self.up()
        outside=self.submit("outside-callback-scope","danger_zone")
        self.wait(outside,"callback_status","unconfigured")
        code,inside=self.api("/api/v1/tasks",dict(self.payload("inside","danger_zone"),project_id="callback-only"))
        assert code==202
        self.wait(inside["task_id"],"callback_status","delivered")
        self.down();self.up()
        assert self.api("/admin/api/live/tasks/"+outside+"/resend",{},**{"X-Admin-Action":"1"})[0]==409
        assert self.api("/api/v1/tasks/"+outside)[1]["callback_status"]=="unconfigured"
        assert all(e["task_id"]!=outside for e in p.events)
        print("PASS scoped no-auth callback, restart isolation and resend guard",flush=True)

    def attempt(self,task,col,state,count,attempt):
        t=self.api("/api/v1/tasks/"+task)[1]
        return t if t[col]==state and t[count]==attempt else None

    def wait_by_alert_callback(self,alert):
        page=self.api("/admin/api/live/tasks?alert="+alert)[1]
        return self.wait(page["items"][0]["task_id"],"callback_status","delivered")


if __name__=="__main__":
    assert os.path.ismount(os.environ.get("FOUR_S_VOLUME","/Volumes/Data"))
    with tempfile.TemporaryDirectory(prefix="pilot-qa-",dir=ROOT/"var") as tmp:
        peer=ThreadingHTTPServer(("127.0.0.1",0),Peer)
        peer.events=[]; peer.model_calls=0; peer.callback_code=200
        peer.model_response="valid";peer.block_review=False;peer.block_delivery=False
        for name in ("review_enter","review_release","delivery_enter","delivery_release"):
            setattr(peer,name,threading.Event())
        threading.Thread(target=peer.serve_forever,daemon=True).start()
        pilot=Pilot(Path(tmp),peer)
        try:
            pilot.run()
        finally:
            pilot.down();pilot.log.close();peer.shutdown();peer.server_close()
