"""Manage the trial tunnel and the independent fixed-address HTTPS listener."""
import ipaddress
import json
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from app.config import HTTPS_BIND, HTTPS_PORT, ROOT, STATE

GATEWAY = ROOT/"scripts/public_gateway.py"
BINARY = STATE/"bin/cloudflared"
TUNNEL_LOG = STATE/"public-tunnel.log"
URL_FILE = STATE/"public-url.txt"
NGROK = STATE / "bin/ngrok"
NGROK_CONFIG = STATE / "ngrok.yml"
NGROK_URL = STATE / "ngrok-url.txt"
NGROK_LOG = STATE / "ngrok.log"


def pid_for(name, entry):
    pidfile = STATE/(name+".pid")
    if pidfile.exists():
        pid = int(pidfile.read_text())
        ps = subprocess.run(["ps","-p",str(pid),"-o","command="],capture_output=True,text=True)
        command = ps.stdout.rstrip()
        if entry == GATEWAY and not command.endswith(str(GATEWAY)):
            return None
        if str(entry) in command:
            return pid


def launch(name, args, log, env=None):
    with log.open("wb") as output:
        child = subprocess.Popen(args, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                 start_new_session=True, env=env)
    (STATE/(name+".pid")).write_text(str(child.pid))
    return child


def stop_process(name, entry):
    pid = pid_for(name, entry)
    if pid:
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if not pid_for(name, entry):
                break
            time.sleep(.1)
        else:
            raise SystemExit("公网进程尚未退出，请检查 " + name)
    (STATE / (name + ".pid")).unlink(missing_ok=True)


def public_stop():
    for name, entry in (("ngrok", NGROK), ("public-tunnel", BINARY), ("public-gateway", GATEWAY)):
        stop_process(name, entry)
    URL_FILE.unlink(missing_ok=True)


def fixed_stop():
    stop_process("fixed-https", str(GATEWAY) + " --tls")


def fixed_start(address=None):
    saved = STATE / "fixed-ip.txt"
    if not address and not saved.exists():
        raise ValueError("首次请指定证书对应的固定 IPv4：./4s fixed-start <IP>")
    address = str(ipaddress.IPv4Address(address or saved.read_text().strip()))
    tls = STATE / "tls"
    cert, key = tls / "fullchain.pem", tls / "privkey.pem"
    if not cert.is_file() or not key.is_file():
        raise ValueError("请先在数据卷 var/tls/ 配置该 IP 的 fullchain.pem 和 privkey.pem")
    # Connect locally and verify the certificate against the IP used by clients.
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=str(cert))
    child = None
    if not pid_for("fixed-https", str(GATEWAY) + " --tls"):
        child = launch("fixed-https", [sys.executable, "-B", str(GATEWAY), "--tls"], STATE / "fixed-https.log")
    try:
        for _ in range(50):
            if child and child.poll() is not None:
                raise RuntimeError("HTTPS 网关启动失败，请检查 var/fixed-https.log")
            try:
                host = "127.0.0.1" if HTTPS_BIND == "0.0.0.0" else HTTPS_BIND
                with socket.create_connection((host, HTTPS_PORT), timeout=1) as conn:
                    with context.wrap_socket(conn, server_hostname=address):
                        break
            except ConnectionRefusedError:
                time.sleep(.1)
        else:
            raise RuntimeError("本机 HTTPS 端口未就绪")
        temp = saved.with_suffix(".new")
        temp.write_text(address + "\n")
        temp.replace(saved)
        return address
    except BaseException:
        if child:
            fixed_stop()
        raise


def public_status():
    if not pid_for("public-gateway", GATEWAY):
        return None
    if pid_for("ngrok", NGROK) and NGROK_URL.is_file():
        return NGROK_URL.read_text().strip()
    if pid_for("public-tunnel",BINARY) and URL_FILE.is_file():
        return URL_FILE.read_text().strip()
    return None


def ensure_gateway(port):
    origin = f"http://127.0.0.1:{port+1}"
    gateway = None
    if not pid_for("public-gateway", GATEWAY):
        gateway = launch("public-gateway", [sys.executable, "-B", str(GATEWAY)], STATE/"public-gateway.log")
    op = build_opener(ProxyHandler({}))
    for _ in range(50):
        if gateway and gateway.poll() is not None:
            raise RuntimeError("公网网关启动失败，见 var/public-gateway.log")
        try:
            with op.open(origin + "/healthz", timeout=2) as r:
                if r.status == 200 and json.load(r) == {"status": "ok"}:
                    return origin
        except (OSError, ValueError):
            pass
        time.sleep(.2)
    raise RuntimeError("收件网关尚未就绪")


def configure_ngrok(token):
    if not NGROK.is_file():
        raise ValueError("请将 ngrok 官方客户端安装在数据卷 var/bin/ngrok")
    if not token or any(c.isspace() for c in token):
        raise ValueError("请使用 ngrok 控制台的 Authtoken")
    stage = NGROK_CONFIG.with_suffix(".new")
    payload = {"version": "3", "agent": {"authtoken": token, "web_addr": False, "update_check": False}}
    try:
        stage.write_text(json.dumps(payload) + "\n")
        stage.chmod(0o600)
        subprocess.run([str(NGROK), "config", "check", "--config", str(stage)],
                       check=True, capture_output=True)
        stage.replace(NGROK_CONFIG)
    finally:
        stage.unlink(missing_ok=True)


def start_ngrok(origin):
    pinned = NGROK_URL.read_text().strip() if NGROK_URL.exists() else None
    allowed = r"https://[a-z0-9-]+\.(?:ngrok-free\.(?:app|dev|pizza)|ngrok\.(?:app|dev|io))"
    if pinned is not None and not re.fullmatch(allowed, pinned):
        raise ValueError("已保存的 ngrok 固定网址无效，请检查 var/ngrok-url.txt")
    if pid_for("ngrok", NGROK):
        if pinned:
            return pinned
        stop_process("ngrok", NGROK)
    if not NGROK.is_file():
        raise ValueError("缺少本机系统对应的 var/bin/ngrok")
    args = [str(NGROK), "http", origin, "--config", str(NGROK_CONFIG), "--inspect=false",
            "--log=stdout", "--log-format=json", "--url", pinned or "https://"]
    env = {k: v for k, v in os.environ.items() if k not in ("NGROK_AUTHTOKEN", "NGROK_API_KEY")}
    # Use the Mac's normal outbound route. Its desktop HTTP proxy is optional,
    # and ngrok bills explicit agent proxying separately from a direct tunnel.
    for key, value in list(env.items()):
        if key.lower() in ("http_proxy", "https_proxy", "all_proxy") and urlsplit(value).hostname in ("127.0.0.1", "localhost", "::1"):
            del env[key]
    env["TMPDIR"] = str(STATE / "tmp")
    child = launch("ngrok", args, NGROK_LOG, env=env)
    try:
        for _ in range(90):
            log = NGROK_LOG.read_text(errors="replace")
            if child.poll() is not None:
                code = re.search(r"ERR_NGROK_\d+", log)
                raise RuntimeError("ngrok 连接失败，请检查账号认证和网络" + (" · " + code[0] if code else ""))
            for line in reversed(log.splitlines()):
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict) or event.get("msg") != "started tunnel":
                    continue
                url = event.get("url", "")
                if not re.fullmatch(allowed, url) or (pinned and url != pinned):
                    raise RuntimeError("ngrok 返回的网址与预期固定域名不符")
                if not pinned:
                    stage = NGROK_URL.with_suffix(".new")
                    stage.write_text(url + "\n")
                    stage.replace(NGROK_URL)
                return url
            time.sleep(.5)
        raise RuntimeError("ngrok 连接超时，固定域名未确认")
    except BaseException:
        stop_process("ngrok", NGROK)
        raise


def public_start(port):
    if NGROK_CONFIG.is_file() or NGROK_URL.exists():
        if not NGROK_CONFIG.is_file():
            raise ValueError("固定域名凭据缺失，请运行 ./4s tunnel-setup")
        return start_ngrok(ensure_gateway(port))
    active = public_status()
    if active:
        return active
    if not BINARY.is_file():
        raise SystemExit("请将官方 cloudflared 可执行文件放在数据目录 bin/cloudflared")
    stop_process("public-tunnel", BINARY)
    try:
        origin = ensure_gateway(port)
        tunnel = launch("public-tunnel",[str(BINARY),"tunnel","--no-autoupdate","--protocol","http2",
                                        "--edge-ip-version","4","--url",origin],TUNNEL_LOG)
        for _ in range(120):
            if tunnel.poll() is not None:
                raise RuntimeError("公网隧道已退出，见 var/public-tunnel.log")
            log = TUNNEL_LOG.read_text(errors="replace")
            match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com\b",log)
            if match and "Registered tunnel connection" in log:
                URL_FILE.write_text(match[0]+"\n")
                return match[0]
            time.sleep(.5)
        raise RuntimeError("公网隧道连接超时，见 var/public-tunnel.log")
    except (Exception, KeyboardInterrupt):
        public_stop()
        raise
