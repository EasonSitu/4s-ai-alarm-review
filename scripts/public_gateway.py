"""Public listener: only forward canonical partner API routes to the local service."""
import argparse
import http.client
import re
import sys
from pathlib import Path

import uvicorn
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import HTTPS_BIND, HTTPS_PORT, STATE, init_config
from app.main import Gate, cfg, fail

READABLE = {"/healthz", "/docs", "/openapi.json", "/api-guide.md", "/api-guide.pdf", "/integration.zip", "/assets/style.css"}
DETAIL = re.compile(r"/api/v1/tasks/[0-9a-f]{32}")


def relay(method, path, headers, data):
    conn = http.client.HTTPConnection("127.0.0.1", cfg["port"], timeout=30)
    try:
        conn.request(method, path, body=data, headers=headers)
        reply = conn.getresponse()
        return Response(reply.read(), status_code=reply.status,
                        headers={"Content-Type": reply.getheader("Content-Type", "application/octet-stream")})
    except (OSError, http.client.HTTPException):
        return fail(503, "Mac 收件服务暂不可用，请保留原编号重试")
    finally:
        conn.close()


async def forward(scope, receive, send):
    req = Request(scope, receive)
    headers = {k: req.headers[k] for k in ("authorization", "content-type") if k in req.headers}
    data = await req.body() if req.method == "POST" else None
    response = await run_in_threadpool(relay, req.method, scope["path"], headers, data)
    await response(scope, receive, send)


guarded = Gate(forward)


async def gateway(scope, receive, send):
    path, method = scope["path"], scope["method"]
    if (method == "POST" and path == "/api/v1/tasks") or (method == "GET" and (path in READABLE or DETAIL.fullmatch(path))):
        return await guarded(scope, receive, send)
    return await fail(404, "此入口仅供平台对接")(scope, receive, send)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="4S 对外收件网关")
    parser.add_argument("--tls", action="store_true", help="使用固定 HTTPS 入口")
    args = parser.parse_args()
    cfg.update(init_config())
    if args.tls:
        tls = STATE / "tls"
        options = dict(host=HTTPS_BIND, port=HTTPS_PORT,
                       ssl_certfile=str(tls / "fullchain.pem"),
                       ssl_keyfile=str(tls / "privkey.pem"))
    else:
        options = dict(host="127.0.0.1", port=cfg["port"] + 1)
    uvicorn.run(gateway, **options, lifespan="off", ws="none",
                proxy_headers=False, access_log=False, limit_concurrency=20, timeout_keep_alive=5)
