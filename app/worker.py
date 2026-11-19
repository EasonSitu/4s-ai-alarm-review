import logging
import threading
import time
from urllib.parse import urlsplit

from .config import CONFIG_LOCK, callback_ready
from .contracts import CALLBACK_DELAYS, REVIEW_DELAYS, RULE, CallbackEvent, local_review_model
from .review import RemoteError, post_json, review
from .store import aggregate, db, frame_results, now, original, view

log = logging.getLogger("uvicorn.error")


def claim(mode, delivery, cfg):
    column, due, attempts = ("callback_status", "callback_due", "callback_attempts") if delivery else ("status", "review_due", "review_attempts")
    states = ("pending", "retrying") if delivery else ("queued", "retrying")
    active = "sending" if delivery else "processing"
    with db(mode) as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(f"SELECT task_id FROM tasks WHERE historical_test=0 AND {column} IN (?,?) AND {due}<=? ORDER BY {due}, received_at LIMIT 1",
                        (*states, time.time())).fetchone()
        if row is None:
            return None
        extra = "" if delivery else ", started_at=COALESCE(started_at, ?)"
        args = (active,) if delivery else (active, now())
        c.execute(f"UPDATE tasks SET {column}=?, {attempts}={attempts}+1 {extra} WHERE task_id=?", (*args, row[0]))
        if not delivery:
            c.execute("UPDATE tasks SET rule_version=? WHERE task_id=?", (RULE,row[0]))
        task = view(mode, c.execute("SELECT * FROM tasks WHERE task_id=?", (row[0],)).fetchone(), c)
        if not delivery:
            local_model = local_review_model(mode, task["project_id"], task["alarm_type"])
            external = local_model is None
            remote = urlsplit(cfg["model_base"]) if external else None
            profile = local_model or cfg.get("active_profile") or "legacy"
            c.execute("""INSERT INTO review_history(task_id,attempt,profile,endpoint_origin,requested_model,rule_version,started_at,status)
                         VALUES(?,?,?,?,?,?,?,'processing')""", (row[0],task["review_attempts"],profile,
                         f"{remote.scheme}://{remote.netloc}" if remote else None,cfg["model"] if external else None,RULE,now()))
        return task


def diagnostic(exc):
    return str(exc) if isinstance(exc, RemoteError) else "处理异常 · " + type(exc).__name__


def call_model(mode, cfg, t, image, role, profile, timeout):
    index = t["frame_index"]
    remote = urlsplit(cfg["model_base"])
    with db(mode) as c:
        c.execute("""INSERT INTO model_call_history
                     (task_id,attempt,image_index,role,profile,endpoint_origin,requested_model,started_at,status)
                     VALUES(?,?,?,?,?,?,?,?,'processing')""",
                  (t["task_id"],t["review_attempts"],index,role,profile,
                   f"{remote.scheme}://{remote.netloc}",cfg["model"],now()))
    try:
        result = review(mode, cfg, t, *image, timeout=timeout)
    except Exception as exc:
        with db(mode) as c:
            c.execute("""UPDATE model_call_history SET status='failed',error=?,finished_at=?
                         WHERE task_id=? AND attempt=? AND role=? AND image_index=?""",
                      (diagnostic(exc),now(),t["task_id"],t["review_attempts"],role,index))
        raise
    with db(mode) as c:
        c.execute("""UPDATE model_call_history SET status='completed',actual_model=?,finished_at=?
                     WHERE task_id=? AND attempt=? AND role=? AND image_index=?""",
                  (result[2],now(),t["task_id"],t["review_attempts"],role,index))
    return result


def review_frame(mode, cfg, task, frame):
    index = frame["index"]
    with db(mode) as c:
        image = original(c, task["task_id"], index)
    inputs = dict(task, frame_index=index, roi=frame["roi"], person_boxes=frame["person_boxes"])
    if local_review_model(mode, task["project_id"], task["alarm_type"]):
        return review(mode, cfg, inputs, *image)
    try:
        return call_model(mode, cfg, inputs, image, "primary", cfg.get("active_profile") or "legacy",
                          cfg.get("primary_timeout_seconds") or 60)
    except RemoteError:
        if not cfg.get("fallback_config"):
            raise
        return call_model(mode, dict(cfg, **cfg["fallback_config"]), inputs, image, "fallback",
                          cfg.get("fallback_profile"), 60)


def run_review(mode, cfg, t, delays=REVIEW_DELAYS):
    start = time.monotonic()
    attempt = t["review_attempts"]
    for frame in t["images"]:
        if frame["status"] in ("completed", "failed"):
            continue
        verdict = reason = model = error = None
        try:
            verdict, reason, model = review_frame(mode, cfg, t, frame)
            state = "completed"
        except Exception as exc:
            error = diagnostic(exc)
            state = "retrying" if attempt <= len(delays) else "failed"
        with db(mode) as c:
            c.execute("""UPDATE task_frames SET status=?,verdict=?,reason=?,model=?,error=?
                         WHERE task_id=? AND idx=?""",
                      (state, verdict, reason, model, error, t["task_id"], frame["index"]))
    with db(mode) as c:
        frames = frame_results(c, t["task_id"])
    retry = next((f for f in frames if f["status"] == "retrying"), None)
    if retry:
        status, verdict, reason, error, model = "retrying", None, None, retry["error"], None
    else:
        status, verdict, reason, error, model = aggregate(frames)
    elapsed = round((time.monotonic()-start)*1000)
    finished = status != "retrying"
    callback = "pending" if callback_ready(cfg, mode) else "unconfigured"
    due = 0 if finished else time.time() + delays[attempt-1]
    with db(mode) as c:
        c.execute("""UPDATE tasks SET status=?,verdict=?,reason=?,model=?,error=?,elapsed_ms=?,finished_at=?,
                   callback_status=?,review_due=? WHERE task_id=?""",
                  (status, verdict, reason, model, error, elapsed, now() if finished else None,
                   callback if finished else "waiting_result", due, t["task_id"]))
        c.execute("UPDATE review_history SET status=?,actual_model=?,finished_at=? WHERE task_id=? AND attempt=?",
                  (status,model if verdict else None,now(),t["task_id"],attempt))


def run_callback(mode, cfg, t, delays=CALLBACK_DELAYS):
    attempt = t["callback_attempts"]
    target, token = cfg["callback_url"], cfg["callback_token"]
    due, error = 0, None
    try:
        if attempt > len(delays) + 1:
            raise RemoteError("中断后已耗尽回调次数")
        post_json(target, token, CallbackEvent.model_validate(t).model_dump(), 10)
        state = "delivered"
    except Exception as exc:
        error = diagnostic(exc)
        state = "failed"
        if attempt <= len(delays):
            due = time.time() + delays[attempt-1]
            state = "retrying"
    with db(mode) as c:
        c.execute("UPDATE tasks SET callback_status=?,callback_error=?,callback_due=? WHERE task_id=?",
                  (state, error, due, t["task_id"]))


def work(stop: threading.Event, mode, cfg, delivery):
    run = run_callback if delivery else run_review
    while not stop.is_set():
        try:
            with CONFIG_LOCK:
                current = dict(cfg)
                task = claim(mode, delivery, current)
            if task is not None:
                run(mode, current, task)
                continue
        except Exception:
            log.exception("队列读写异常 %s %s", mode, "回调" if delivery else "审核")
        stop.wait(0.7)
