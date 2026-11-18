"""Check the service health without submitting an image."""
import argparse
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit

from submit import api_client, call


def main():
    parser = argparse.ArgumentParser(description="检查复核服务是否在线")
    parser.add_argument("--base", required=True, help="服务的 HTTPS 根地址")
    base = parser.parse_args().base.rstrip("/")
    parts = urlsplit(base)
    if parts.path or parts.query or parts.fragment:
        parser.error("请输入服务根地址")
    try:
        status, result = call(api_client(base), base + "/healthz")
        if status != 200 or result != {"status": "ok"}:
            sys.exit("服务未就绪")
        print('HTTP 200 {"status":"ok"}')
    except HTTPError as exc:
        sys.exit(f"服务不可用，HTTP {exc.code}")
    except (OSError, ValueError) as exc:
        sys.exit("连接失败：" + str(exc))


if __name__ == "__main__":
    main()
