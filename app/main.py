import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import date
from html import escape
from string import Template
from time import monotonic
from typing import Literal, Optional
from urllib.parse import urlsplit

import markdown
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import store
from .config import acquire_file_lock, CONFIG_LOCK, DISK, MAX_BODY, ROOT, STATE, admin_password, callback_ready, init_config, model_ready, save_config
from .contracts import CallbackEvent, Delivery, Health, RequestError, Status, Intake, Task, Verdict, unpack_alarm
from .model_catalog import list_models as provider_catalog, valid_base
from .model_profiles import MODEL_FIELDS, probe, with_profile
from .review import RemoteError
from .worker import work

cfg = {}
LOCAL = {"localhost", "127.0.0.1", "::1"}
Lane = Literal["live"]


@asynccontextmanager
async def lifespan(app):
    cfg.update(init_config())
    with (STATE / "worker.lock").open("a+") as lock:
        acquire_file_lock(lock, blocking=False)
        stop = threading.Event()
        threads = []
        store.init("live", cfg)
        for delivery in (False, True):
            threads.append(threading.Thread(target=work, args=(stop, "live", cfg, delivery), daemon=True))
        for t in threads:
            t.start()
        try:
            yield
        finally:
            stop.set()
            # Startup recovers active records if the process was killed before commit.
            for t in threads:
                await run_in_threadpool(t.join, 1)


api = FastAPI(title="4S 报警复核接口", version="1.8.0", docs_url=None, redoc_url=None, lifespan=lifespan)
bearer = HTTPBearer(auto_error=False)


def eq(a, b):
    return hmac.compare_digest(a.encode(), b.encode())


def fail(code, message):
    return JSONResponse({"detail": message}, status_code=code)


class Gate:
    def __init__(self, app, *, public_admin_origin=None):
        self.app = app
        self.public_admin_origin = public_admin_origin
        self.login_failures = OrderedDict()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        req = Request(scope)
        path = scope["path"]
        rejection = None
        if path.startswith(("/admin", "/internal")):
            if self.public_admin_origin is None:
                forwarded = any(k in req.headers for k in ("forwarded","x-forwarded-for","x-forwarded-host","x-real-ip","cf-connecting-ip"))
                if forwarded or req.client is None or req.client.host not in LOCAL or req.url.hostname not in LOCAL:
                    rejection = fail(403, "内部页面仅限服务器本机访问")
                allowed_origin = f"{scope['scheme']}://{req.headers.get('host')}"
            else:
                allowed_origin = self.public_admin_origin
                local_tls_proxy = (req.client and req.client.host in LOCAL
                                   and req.headers.get("x-forwarded-proto") == "https")
                if (not allowed_origin or "https://" + req.headers.get("host", "") != allowed_origin
                        or not (scope["scheme"] == "https" or local_tls_proxy)
                        or len(admin_password(cfg)) < 10):
                    rejection = fail(404, "未开放此入口")
            if rejection is None and path.startswith("/admin"):
                client_ip = req.client.host if req.client else ""
                if self.public_admin_origin and scope["scheme"] == "http" and client_ip in LOCAL:
                    # The loopback HTTPS proxy appends the immediate peer as the last hop.
                    client_ip = req.headers.get("x-forwarded-for", client_ip).rsplit(",", 1)[-1].strip()
                now = monotonic()
                failures, until = self.login_failures.get(client_ip, (0, 0))
                if until <= now:
                    failures, until = 0, now + 60
                expected = base64.b64encode(("admin:"+admin_password(cfg)).encode()).decode()
                if self.public_admin_origin and failures >= 8:
                    rejection = fail(429, "登录尝试过多，请稍后再试")
                    rejection.headers["Retry-After"] = str(max(1, int(until - now) + 1))
                elif not eq(req.headers.get("authorization",""), "Basic "+expected):
                    if self.public_admin_origin and req.headers.get("authorization"):
                        self.login_failures[client_ip] = (failures + 1, until)
                        self.login_failures.move_to_end(client_ip)
                        if len(self.login_failures) > 4096:
                            self.login_failures.popitem(last=False)
                    rejection = fail(401, "请使用后台账户 admin 登录")
                    rejection.headers["WWW-Authenticate"] = 'Basic realm="4S admin"'
                    rejection.headers["Cache-Control"] = "no-store"
                elif self.public_admin_origin:
                    self.login_failures.pop(client_ip, None)
            if rejection is None and req.method == "POST" and path.startswith("/admin"):
                origin = req.headers.get("origin", "")
                if req.headers.get("x-admin-action") != "1" or (origin and origin != allowed_origin):
                    rejection = fail(403, "请从审核后台操作")
        if path.startswith("/api/v1/"):
            if not eq(req.headers.get("authorization", ""), "Bearer " + cfg["live_token"]):
                rejection = fail(401, "环境或接口令牌不正确")
        if req.method == "POST" and rejection is None:
            declared = req.headers.get("content-length", "")
            if declared and (not declared.isdigit() or len(declared)>9 or int(declared)>MAX_BODY):
                rejection = fail(413, "请求超过 56 MiB")
            elif path.endswith("/tasks") and req.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
                rejection = fail(415, "请发送 application/json")
        if rejection is not None:
            return await rejection(scope, receive, send)
        if req.method == "POST":
            buf = bytearray()
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    return
                buf.extend(msg.get("body", b""))
                if len(buf) > MAX_BODY:
                    return await fail(413, "请求超过 56 MiB")(scope, receive, send)
                if not msg.get("more_body"):
                    break
            original = receive
            async def buffered():
                nonlocal buf
                if buf is None:
                    return await original()
                body = bytes(buf)
                buf = None
                return {"type": "http.request", "body": body, "more_body": False}
            receive = buffered
        async def headers(msg):
            if msg["type"] == "http.response.start":
                msg["headers"] += [(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                                   (b"x-frame-options", b"DENY"), (b"referrer-policy", b"no-referrer")]
            await send(msg)
        return await self.app(scope, receive, headers)


api.add_middleware(Gate)


@api.exception_handler(RequestValidationError)
async def invalid(req, exc):
    # Never echo request bodies (images) into validation responses.
    if any(e["type"] == "string_too_long" and e["loc"][-1:] == ("image_base64",) for e in exc.errors()):
        return fail(413, "图片超过 8 MiB")
    errors = [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()[:8]]
    return JSONResponse({"detail": errors}, status_code=422)


@api.exception_handler(sqlite3.OperationalError)
async def unavailable(req, exc):
    return fail(503, "数据库暂不可写或不可读，请检查数据盘并原样重试")


def submit(mode, s, response):
    try:
        meta, data, mime, signature, extras = unpack_alarm(s)
        with CONFIG_LOCK:
            task, fresh = store.add(mode, cfg, meta, data, mime, signature, extras)
    except OverflowError as exc:
        raise HTTPException(413, str(exc))
    except ValueError as exc:
        raise HTTPException(422, [{"loc": ["body", "image_base64"], "msg": str(exc)}])
    except store.Conflict:
        raise HTTPException(409, "该项目已接收此报警编号，图片、顺序或元数据与原件不同")
    response.status_code = 202 if fresh else 200
    return task


def lookup(mode, task_id):
    task = store.get(mode, task_id)
    if task is None:
        raise HTTPException(404, "当前环境查无此任务")
    return task


def routes(mode, prefix):
    @api.post(prefix + "/tasks", status_code=202, tags=[mode], response_model=Task, dependencies=[Depends(bearer)],
              include_in_schema=mode=="live", operation_id=mode+"_submit", summary="接收一条报警及 1–5 张图片",
              responses={200: {"model": Task, "description":"相同报警图片已接收"}, 409: {"description":"相同编号内容冲突"},
                         401: {"description":"令牌错误"}, 413: {"description":"请求过大"}, 415: {"description":"格式错误"},
                         422: {"model": RequestError, "description": "字段、图片或 ROI 无效"},
                         503: {"description":"收件服务暂不可用"}})
    def intake(s: Intake, response: Response):
        return submit(mode, s, response)

    @api.get(prefix + "/tasks/{task_id}", tags=[mode], response_model=Task, dependencies=[Depends(bearer)],
             include_in_schema=mode=="live", summary="查询审核和回调状态", operation_id=mode+"_get",
             responses={401: {"description": "令牌错误"}, 404: {"description":"当前环境查无任务"},
                        503: {"description": "服务暂不可用"}})
    def get_task(task_id: str):
        return lookup(mode, task_id)


routes("live", "/api/v1")


@api.webhooks.post("review.finished", summary="审核结束后向 4S 的固定地址投递", dependencies=[Depends(bearer)])
def result_callback(event: CallbackEvent):
    """接收方持久化后返回 2xx，按 mode 和 task_id 去重。此地址由 4S 提供。"""


@api.get("/healthz", summary="检查服务是否就绪", response_model=Health,
         responses={503: {"description": "服务暂不可用"}})
def health():
    if not os.path.ismount(DISK):
        raise HTTPException(503, "服务暂不可用")
    try:
        with store.db("live") as c:
            c.execute("SELECT task_id FROM tasks LIMIT 1").fetchone()
    except sqlite3.Error:
        raise HTTPException(503, "服务暂不可用") from None
    return {"status": "ok"}


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=48)
    model_base: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=160)
    model_key: Optional[str] = Field(default=None, max_length=4096, repr=False)
    provider: Literal["deepseek", "bailian", "compatible"] = "compatible"


class ModelChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=48)
    revision: str = Field(min_length=64, max_length=64)


class CatalogChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["deepseek", "bailian", "compatible"]
    name: Optional[str] = Field(default=None, max_length=48)
    model_base: str = Field(min_length=1, max_length=2048)
    model_key: Optional[str] = Field(default=None, max_length=4096, repr=False)


class RoutingChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    primary: str = Field(min_length=1, max_length=48)
    primary_revision: str = Field(min_length=64, max_length=64)
    fallback: Optional[str] = Field(default=None, max_length=48)
    fallback_revision: Optional[str] = Field(default=None, max_length=64)
    timeout_seconds: int = Field(default=15, ge=1, le=120)


def provider_name(profile):
    if profile.get("provider") in ("deepseek", "bailian", "compatible"):
        return profile["provider"]
    host = (urlsplit(profile.get("model_base", "")).hostname or "").lower()
    if host == "api.deepseek.com":
        return "deepseek"
    if host.endswith(".aliyuncs.com"):
        return "bailian"
    return "compatible"


def model_revision(saved, name):
    profile = saved["model_profiles"][name]
    data = json.dumps([name, *(profile[key] for key in MODEL_FIELDS), provider_name(profile)], ensure_ascii=False).encode()
    return hmac.new(saved["live_token"].encode(), data, hashlib.sha256).hexdigest()


def saved_models():
    saved = json.loads((STATE / "config.json").read_text(encoding="utf-8"))
    if not saved.get("active_profile") and model_ready(cfg):
        saved.setdefault("model_profiles", {}).setdefault("legacy", {key: cfg[key] for key in MODEL_FIELDS})
    return saved


@api.get("/admin/api/models", include_in_schema=False)
def list_models():
    with CONFIG_LOCK:
        saved = saved_models()
        profiles = [
            {"name": name, "model": profile["model"], "model_base": profile["model_base"],
             "provider": provider_name(profile), "has_key": bool(profile.get("model_key")),
             "revision": model_revision(saved, name)}
            for name, profile in sorted(saved.get("model_profiles", {}).items())
        ]
        return {"active_profile": cfg.get("active_profile") or ("legacy" if model_ready(cfg) else "off"),
                "active_model": cfg["model"] if model_ready(cfg) else None,
                "fallback_profile": cfg.get("fallback_profile") if model_ready(cfg) else None,
                "fallback_model": (cfg.get("fallback_config") or {}).get("model") if model_ready(cfg) else None,
                "preferred_primary": saved.get("preferred_primary"),
                "preferred_fallback": saved.get("preferred_fallback"),
                "timeout_seconds": cfg.get("primary_timeout_seconds") or saved.get("primary_timeout_seconds") or 15,
                "profiles": profiles}


@api.post("/admin/api/models/catalog", include_in_schema=False)
def model_catalog(form: CatalogChoice):
    base = form.model_base.strip().rstrip("/")
    key = (form.model_key or "").strip()
    try:
        parsed = valid_base(base)
        if not key and form.name:
            with CONFIG_LOCK:
                old = saved_models().get("model_profiles", {}).get(form.name)
            if old:
                previous = urlsplit(old["model_base"])
                if (previous.scheme, previous.hostname, previous.port) != (parsed.scheme, parsed.hostname, parsed.port):
                    raise ValueError("更换服务地址时，请重新输入对应供应商的 API Key")
                key = old.get("model_key", "")
        return provider_catalog(form.provider, base, key)
    except (ValueError, RemoteError) as exc:
        raise HTTPException(400, str(exc)) from None


@api.post("/admin/api/models/save", include_in_schema=False)
def save_model(form: ModelSettings):
    name = form.name.strip()
    model = form.model.strip()
    base = form.model_base.strip().rstrip("/")
    key = (form.model_key or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", name) or name == "off":
        raise HTTPException(422, "配置名称请使用字母、数字、横线或下划线")
    if not model:
        raise HTTPException(422, "请填写模型 ID")
    try:
        parsed = valid_base(base)
        with (STATE / "control.lock").open("a+") as lock:
            acquire_file_lock(lock)
            with CONFIG_LOCK:
                saved = saved_models()
                profiles = saved.setdefault("model_profiles", {})
                old = profiles.get(name, {})
                if not key and old:
                    previous = urlsplit(old["model_base"])
                    if (previous.scheme, previous.hostname, previous.port) != (parsed.scheme, parsed.hostname, parsed.port):
                        raise ValueError("更换服务地址时，请重新输入对应供应商的 API Key")
                    key = old.get("model_key", "")
                if not key:
                    raise ValueError("请输入 API Key")
                profiles[name] = {"model_base": base, "model": model, "model_key": key, "provider": form.provider}
                with_profile(saved, name)
                save_config(saved)
                cfg["model_profiles"] = profiles
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"saved": True, "name": name, "revision": model_revision(saved, name)}


def activate_config(saved, previous):
    with CONFIG_LOCK:
        try:
            with store.db("live") as c:
                c.execute("BEGIN IMMEDIATE")
                save_config(saved)
                if model_ready(saved):
                    c.execute("UPDATE tasks SET status='queued' WHERE historical_test=0 AND status='awaiting_model'")
                else:
                    c.execute("""UPDATE tasks SET status='awaiting_model'
                                 WHERE status IN ('queued','retrying') AND alarm_type IN ('no_helmet','no_vest')
                                 AND historical_test=0""")
        except Exception:
            save_config(previous)
            raise
        cfg.update({key: saved[key] for key in (*MODEL_FIELDS, "active_profile")})
        cfg["model_profiles"] = saved.get("model_profiles", {})
        for key in ("fallback_profile", "fallback_config", "primary_timeout_seconds", "preferred_primary", "preferred_fallback"):
            cfg[key] = saved.get(key)


@api.post("/admin/api/models/activate", include_in_schema=False)
def activate_model(form: ModelChoice):
    with (STATE / "control.lock").open("a+") as lock:
        acquire_file_lock(lock)
        saved = saved_models()
        try:
            candidate = with_profile(saved, form.name)
            if not eq(form.revision, model_revision(saved, form.name)):
                raise HTTPException(409, "配置已被修改，请重新选择并验证")
            result = probe(candidate)
        except (ValueError, RemoteError) as exc:
            raise HTTPException(400, str(exc)) from None
        candidate.update(fallback_profile=None, fallback_config=None, primary_timeout_seconds=60,
                         preferred_primary=form.name, preferred_fallback=None)
        activate_config(candidate, saved)
    return {"active_profile": form.name, "active_model": candidate["model"], "check": result}


@api.post("/admin/api/models/routing", include_in_schema=False)
def activate_routing(form: RoutingChoice):
    if form.fallback and form.primary == form.fallback:
        raise HTTPException(422, "主模型和备用模型请选择不同的配置")
    with (STATE / "control.lock").open("a+") as lock:
        acquire_file_lock(lock)
        saved = saved_models()
        try:
            primary = with_profile(saved, form.primary)
            if not eq(form.primary_revision, model_revision(saved, form.primary)):
                raise HTTPException(409, "主模型配置已改变，请重新选择")
            backup = None
            if form.fallback:
                backup = with_profile(saved, form.fallback)
                if not form.fallback_revision or not eq(form.fallback_revision, model_revision(saved, form.fallback)):
                    raise HTTPException(409, "备用模型配置已改变，请重新选择")
            primary_check = probe(primary)
            fallback_check = probe(backup) if backup else None
        except (ValueError, RemoteError) as exc:
            raise HTTPException(400, str(exc)) from None
        primary.update(fallback_profile=form.fallback or None,
                       fallback_config={key: backup[key] for key in MODEL_FIELDS} if backup else None,
                       primary_timeout_seconds=form.timeout_seconds,
                       preferred_primary=form.primary, preferred_fallback=form.fallback or None)
        activate_config(primary, saved)
    return {"active_profile": form.primary, "fallback_profile": form.fallback or None,
            "timeout_seconds": form.timeout_seconds, "primary_check": primary_check, "fallback_check": fallback_check}


@api.post("/admin/api/models/pause", include_in_schema=False)
def pause_models():
    with (STATE / "control.lock").open("a+") as lock:
        acquire_file_lock(lock)
        saved = saved_models()
        activate_config(dict(saved, active_profile="off", model="", model_base="", model_key=""), saved)
    return {"active_profile": "off"}


@api.get("/admin/api/{mode}/tasks", include_in_schema=False)
def admin_list(mode: Lane, project: str="", alert: str="", status: Optional[Status]=None, verdict: Optional[Verdict]=None,
               callback: Optional[Delivery]=None, since: Optional[date]=None, until: Optional[date]=None,
               page: int=Query(default=1,ge=1,le=1_000_000)):
    visible = "(historical_test=0 OR (model IS NOT NULL AND model NOT IN ('test-fixed','mock-fixed')))"
    where, args = [visible], []
    for field, val in [("status", status), ("verdict", verdict), ("callback_status", callback)]:
        if val:
            where.append(field+"=?")
            args.append(val)
    for field, val in [("project_id", project), ("alert_id", alert)]:
        if val:
            where.append("instr("+field+",?)>0")
            args.append(val)
    if since:
        where.append("received_at>=?")
        args.append(since.isoformat())
    if until:
        where.append("substr(received_at,1,10)<=?")
        args.append(until.isoformat())
    clause = " WHERE "+" AND ".join(where) if where else ""
    with store.db(mode) as c:
        c.execute("BEGIN")
        counts = dict(c.execute("SELECT status,count(*) FROM tasks WHERE "+visible+" GROUP BY status").fetchall())
        failed_callbacks = c.execute("SELECT count(*) FROM pilot_tasks WHERE callback_status='failed'").fetchone()[0]
        total = c.execute("SELECT count(*) FROM tasks"+clause, args).fetchone()[0]
        rows = c.execute("SELECT * FROM tasks"+clause+" ORDER BY received_at DESC, rowid DESC LIMIT 25 OFFSET ?",
                         (*args,(page-1)*25)).fetchall()
        items = [store.view(mode, r, c) for r in rows]
    return {"items":items, "total":total, "counts":counts,
            "model_ready":model_ready(cfg), "model_name":cfg["model"] if model_ready(cfg) else None,
            "fallback_model":(cfg.get("fallback_config") or {}).get("model") if model_ready(cfg) else None,
            "callback_ready":callback_ready(cfg,mode), "failed_callbacks":failed_callbacks}


@api.get("/admin/api/{mode}/tasks/{task_id}", include_in_schema=False)
def detail(mode: Lane, task_id: str):
    task = lookup(mode, task_id)
    with store.db(mode) as c:
        calls = c.execute("""SELECT attempt,image_index,role,profile,requested_model,actual_model,started_at,finished_at,status,error
                             FROM model_call_history WHERE task_id=?
                             ORDER BY attempt,image_index,CASE role WHEN 'primary' THEN 0 ELSE 1 END""", (task_id,)).fetchall()
    return dict(task, model_calls=[dict(call) for call in calls])


@api.get("/admin/api/{mode}/tasks/{task_id}/image", include_in_schema=False)
def original(mode: Lane, task_id: str, index: int=Query(default=1,ge=1,le=5)):
    with store.db(mode) as c:
        image = store.original(c, task_id, index)
    if image is None:
        raise HTTPException(404)
    return Response(image["data"], media_type=image["mime"])


@api.post("/admin/api/{mode}/tasks/{task_id}/resend", include_in_schema=False)
def resend(mode: Lane, task_id: str):
    if not callback_ready(cfg,mode):
        raise HTTPException(409, "请先配置回调地址及令牌并重启服务")
    with store.db(mode) as c:
        result = c.execute("""UPDATE tasks SET callback_status='pending',callback_attempts=0,callback_due=0,
                           callback_error=NULL WHERE task_id=? AND historical_test=0
                           AND project_id=COALESCE(?,project_id) AND callback_status IN ('failed','unconfigured')""",
                           (task_id,cfg.get("callback_test_project")))
        if not result.rowcount:
            raise HTTPException(409, "仅可重发失败或未配置的回调")
    return {"queued":True}


@api.post("/admin/api/{mode}/tasks", include_in_schema=False)
def admin_upload(mode: Lane, s: Intake, response: Response):
    return submit(mode, s, response)


def guide_text():
    return (ROOT / "docs/4S-API.md").read_text(encoding="utf-8")


@api.get("/docs", include_in_schema=False)
def docs():
    title, _, source = guide_text().partition("\n")
    content = markdown.markdown(source, extensions=["tables", "fenced_code"])
    content = content.replace("<table>", '<div class="table-wrap"><table>').replace("</table>", "</table></div>")
    template = Template((ROOT / "web/api-docs.html").read_text(encoding="utf-8"))
    return HTMLResponse(template.substitute(title=escape(title.removeprefix("# ")), content=content))


@api.get("/api-guide.pdf", include_in_schema=False)
def guide_pdf():
    return FileResponse(ROOT/"docs/4S-API.pdf", media_type="application/pdf", filename="4S-API.pdf")


@api.get("/integration.zip", include_in_schema=False)
def integration_kit():
    return FileResponse(ROOT/"docs/4S-联调包.zip", media_type="application/zip", filename="4S-integration-v1.zip")


@api.get("/api-guide.md", include_in_schema=False)
def guide_file():
    return Response(guide_text(), media_type="text/markdown; charset=utf-8")


@api.get("/assets/{name}", include_in_schema=False)
def asset(name: str):
    if name not in ("style.css","admin.js"):
        raise HTTPException(404)
    return FileResponse(ROOT/"web"/name)


@api.get("/", include_in_schema=False)
@api.get("/admin", include_in_schema=False)
def home(req: Request):
    if req.url.path == "/":
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/admin" if req.url.hostname in LOCAL else "/docs")
    return FileResponse(ROOT/"web/admin.html")
