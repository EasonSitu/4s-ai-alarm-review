"""4S sender example, standard Python 3.8+. Set FOUR_S_TOKEN before invoking."""
import argparse
import base64
import getpass
import json
import os
import sys
import time
from pathlib import Path
from urllib import error, request
from urllib.parse import urlsplit


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api_client(base):
    parts = urlsplit(base)
    local = parts.hostname in ("127.0.0.1", "localhost", "::1")
    if not parts.hostname or parts.username or parts.password or (parts.scheme != "https" and not (local and parts.scheme == "http")):
        raise ValueError("请使用服务的 HTTPS 地址")
    handlers = [NoRedirect()]
    if local:
        handlers.append(request.ProxyHandler({}))
    return request.build_opener(*handlers)


def call(client, url, token=None, data=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers)
    with client.open(req, timeout=15) as response:
        return response.status, json.load(response)


def read_token():
    return os.getenv("FOUR_S_TOKEN") or getpass.getpass("4S 接口令牌（输入隐藏） ").strip()


def main():
    p = argparse.ArgumentParser(description="提交一张报警图片")
    p.add_argument("image", type=Path)
    p.add_argument("--base", required=True, help="含 /api/v1 的接口地址")
    p.add_argument("--project", required=True, help="4S 提供的项目编号")
    p.add_argument("--alert", required=True)
    p.add_argument("--type", default="no_helmet", choices=["no_helmet","no_vest","danger_zone","combined"])
    p.add_argument("--wait", action="store_true", help="最多等待 90 秒，输出审核结果和当前回调状态")
    args = p.parse_args()
    token = read_token()
    if not token:
        p.error("请先设置环境变量 FOUR_S_TOKEN")
    if not args.image.is_file() or args.image.stat().st_size > 8 * 1024 * 1024:
        p.error("请选择 8 MiB 以内的图片文件")
    payload = dict(project_id=args.project, alert_id=args.alert, alarm_type=args.type,
                   image_base64=base64.b64encode(args.image.read_bytes()).decode())
    try:
        client = api_client(args.base)
        url = args.base.rstrip("/") + "/tasks"
        status, task = call(client, url, token, json.dumps(payload).encode())
        print(f"HTTP {status}", file=sys.stderr)
        print(json.dumps(task, ensure_ascii=False, indent=2))
        if args.wait:
            end = time.monotonic()+90
            while task["status"] not in ("completed","failed","awaiting_model"):
                if time.monotonic() >= end:
                    sys.exit("已收件，仍在处理，请用原任务号查询")
                time.sleep(.8)
                _, task = call(client, url+"/"+task["task_id"], token)
            print("最终查询", json.dumps(task, ensure_ascii=False, indent=2))
    except error.HTTPError as exc:
        sys.exit(f"HTTP {exc.code}: "+exc.read(2048).decode(errors="replace"))
    except OSError as exc:
        sys.exit("连接失败，请确认服务地址和网络："+str(exc))
    except ValueError as exc:
        sys.exit(str(exc))

if __name__=="__main__":
    main()
