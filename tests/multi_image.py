"""Alarm-level contract, aggregation, per-image evidence, retry and recovery on loopback."""
import base64
import concurrent.futures
import io
import json
import sqlite3
import tempfile
import threading
from collections import Counter
from pathlib import Path

from PIL import Image
from pilot import Peer, Pilot, ROOT, ThreadingHTTPServer, until


class ImagePeer(Peer):
    def do_POST(self):
        if self.path != "/v1/chat/completions":
            return super().do_POST()
        content = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["messages"][0]["content"]
        raw = base64.b64decode(content[0]["image_url"]["url"].split(",")[1])
        with Image.open(io.BytesIO(raw)) as img:
            width = img.width
        self.server.calls[width] += 1
        if width == 40:
            return self.reply(503, {})
        evidence = dict(relevant_person_visible=True, is_site_image=True, saw_target_violation=width==20,
                        can_rule_out_every_target_violation=width==30, reason=f"样本 {width}")
        return self.reply(200, {"model":"offline-vision","choices":[{"message":{"content":json.dumps(evidence)}}]})


def photo(width):
    out = io.BytesIO()
    Image.new("RGB", (width, 32)).save(out, "PNG")
    return {"image_base64":base64.b64encode(out.getvalue()).decode()}


def run(state):
    with ThreadingHTTPServer(("127.0.0.1",0), ImagePeer) as peer:
        peer.events, peer.block_delivery, peer.callback_code, peer.calls = [], False, 200, Counter()
        threading.Thread(target=peer.serve_forever, daemon=True).start()
        site = Pilot(state, peer)
        endpoint = f"http://127.0.0.1:{peer.server_port}"
        site.cfg.update(callback_url=endpoint+"/callback", callback_token="qa-callback",
                        model_base=endpoint+"/v1", model_key="fixture")
        try:
            site.up()
            path = "/api/v1/tasks"
            frames = [photo(10),dict(photo(80),roi=[0,0,.25,1]),dict(photo(30),person_boxes=[[0,0,1,1]])]
            req = dict(project_id="qa", alert_id="mixed", alarm_type="no_helmet",images=frames)
            with concurrent.futures.ThreadPoolExecutor(3) as pool:
                responses = list(pool.map(lambda _:site.api(path,req),range(3)))
            assert sorted(code for code,_ in responses)==[200,200,202]
            ids={r["task_id"] for _,r in responses}
            assert len(ids)==1
            task=ids.pop()
            result=site.wait(task,"callback_status","delivered")
            assert result["verdict"]=="valid" and "第 2 张" in result["reason"]
            assert [f["verdict"] for f in result["images"]]==["uncertain","valid","false_alarm"]
            assert result["images"][2]["person_boxes"]==[[0,0,1,1]]
            assert len(peer.events)==1 and peer.events[0]["images"]==result["images"]
            assert peer.calls=={10:1,20:1,30:1}
            detail=site.api("/admin/api/live/tasks/"+task)[1]
            assert [r["image_index"] for r in detail["model_calls"]]==[1,2,3]
            assert site.api("/admin/api/live/tasks/"+task+"/image?index=2")[1]==base64.b64decode(frames[1]["image_base64"])
            assert site.api(path,dict(req,images=frames[::-1]))[0]==409
            assert site.api(path,dict(site.payload("mixed"),image_id="new"))[0]==409
            for invalid in (dict(req,images=[]),dict(req,images=frames*2),dict(req,roi=[0,0,1,1]),
                            dict(req,images=[frames[0],{"image_base64":"YWJj"}])):
                assert site.api(path,dict(invalid, alert_id="invalid"))[0]==422
            with sqlite3.connect(state/"live/tasks.sqlite3") as c:
                assert not c.execute("SELECT 1 FROM tasks WHERE alert_id='invalid'").fetchone()

            for case,widths,verdict in (("clear",[30]*5,"false_alarm"),("unclear",[10,30],"uncertain"),
                                        ("partial",[30,40],"uncertain"),("valid-error",[20,40],"valid"),
                                        ("outage",[40,40],None)):
                payload=dict(req,alert_id=case,images=[photo(w) for w in widths])
                counts=peer.calls.copy()
                code,r=site.api(path,payload);assert code==202
                task=r["task_id"]
                if 40 in widths:
                    for attempt in (1,2):
                        until(lambda:site.attempt(task,"status","retrying","review_attempts",attempt))
                        if case=="partial" and attempt==1:
                            site.down(hard=True);site.up()
                        site.advance(task,"review_due")
                result=site.wait(task,"callback_status","delivered")
                assert result["verdict"]==verdict and result["status"]==("completed" if verdict else "failed")
                assert len([e for e in peer.events if e["task_id"]==task])==1
                for width in set(widths):
                    assert peer.calls[width]-counts[width]==widths.count(width)*(3 if width==40 else 1)
                assert site.api(path,payload)[1]["task_id"]==task
            # Simulate power loss on the last round after a valid frame was durably saved.
            site.down()
            with sqlite3.connect(state/"live/tasks.sqlite3") as c:
                original_task = next(e["task_id"] for e in peer.events if e["alert_id"]=="mixed")
                c.execute("UPDATE tasks SET status='processing',review_attempts=3,verdict=NULL,callback_status='waiting_result' WHERE task_id=?", (original_task,))
                c.execute("UPDATE task_frames SET status='pending',verdict=NULL,reason=NULL WHERE task_id=? AND idx=3", (original_task,))
            site.up()
            recovered=site.wait(original_task,"callback_status","delivered")
            assert recovered["verdict"]=="valid" and recovered["images"][2]["status"]=="failed"
            print("PASS alarm idempotency, frame ROI/originals, validation, 5-image limit, aggregation, recovery, one callback")
        finally:
            site.down();site.log.close();peer.shutdown()


if __name__=="__main__":
    with tempfile.TemporaryDirectory(prefix="multi-image-",dir=ROOT/"var") as tmp:
        run(Path(tmp))
