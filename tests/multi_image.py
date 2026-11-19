"""Exercise multi-image submissions, callback identity and replay after restart."""
import concurrent.futures
import tempfile
import threading
from pathlib import Path

from pilot import Peer, Pilot, ROOT, ThreadingHTTPServer


def run(state):
    with ThreadingHTTPServer(("127.0.0.1", 0), Peer) as peer:
        peer.events, peer.block_delivery, peer.callback_code = [], False, 200
        threading.Thread(target=peer.serve_forever, daemon=True).start()
        site = Pilot(state, peer)
        site.cfg.update(callback_url=f"http://127.0.0.1:{peer.server_port}/callback", callback_token="qa-callback")
        try:
            site.up()
            first = site.payload("same-alert", "danger_zone")
            a, b = dict(first, image_id="01"), dict(first, image_id="02")
            with concurrent.futures.ThreadPoolExecutor(3) as pool:
                results = list(pool.map(lambda q: site.api("/api/v1/tasks", q), (a, b, a)))
            assert sorted(code for code, _ in results) == [200, 202, 202]
            ids = {t["image_id"]: t["task_id"] for _, t in results}
            assert len(set(ids.values())) == 2
            for image_id, task in ids.items():
                result = site.wait(task, "callback_status", "delivered")
                assert result["image_id"] == image_id and result["verdict"] == "uncertain"
            assert {(e["image_id"], e["task_id"]) for e in peer.events} == set(ids.items())
            site.down()
            site.up()
            for data in (a, b):
                code, replay = site.api("/api/v1/tasks", data)
                assert code == 200 and replay["task_id"] == ids[data["image_id"]]
            assert site.api("/api/v1/tasks", dict(a, camera_id="changed"))[0] == 409
            assert site.api("/api/v1/tasks", dict(b, image_id=" "))[0] == 422
            assert site.api("/api/v1/tasks", dict(b, image_id=""))[0] == 422
            print("PASS separate tasks, concurrent replay, per-image callback and restart")
        finally:
            site.down()
            site.log.close()
            peer.shutdown()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="multi-image-", dir=ROOT/"var") as tmp:
        run(Path(tmp))
