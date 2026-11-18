import argparse
import fcntl
import getpass
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import HTTPS_PORT, ROOT, STATE, admin_password, init_config, model_ready, save_config
from app.model_profiles import MODEL_FIELDS, probe, with_profile
from app.review import RemoteError

cfg = init_config()
PID = STATE / "service.pid"
entry = ROOT / "scripts" / "serve.py"
address = "http://127.0.0.1:" + str(cfg["port"])
opener = build_opener(ProxyHandler({}))


def process():
    if not PID.exists():
        return None
    pid = int(PID.read_text())
    command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    if str(entry) in command:
        return pid
    return None


def health():
    try:
        with opener.open(address + "/healthz", timeout=2) as res:
            return json.load(res).get("status") == "ok"
    except (ValueError, OSError):
        return False


def start():
    if process():
        if not health():
            raise SystemExit("进程存在但健康检查失败，请检查 var/service.log")
    else:
        with (STATE/"service.log").open("ab") as log:
            child = subprocess.Popen([sys.executable, "-B", str(entry)], cwd=ROOT, start_new_session=True,
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        PID.write_text(str(child.pid))
        for _ in range(80):
            if child.poll() is not None:
                raise SystemExit("服务启动失败，请检查 Data 盘内的 var/service.log")
            if health():
                break
            time.sleep(.15)
        else:
            raise SystemExit("启动尚未就绪，请查看 var/service.log")
    print("服务已启动", address + "/admin", address + "/docs")


def stop():
    pid = process()
    if pid:
        os.kill(pid, signal.SIGTERM)
        for _ in range(120):
            if process() is None:
                break
            time.sleep(.15)
        else:
            raise SystemExit("服务仍在退出，请查看 var/service.log")
    if PID.exists():
        PID.unlink()
    print("服务已停止")


def save(saved):
    save_config(saved)


def check_and_select(saved, name, select):
    candidate = with_profile(saved, name)
    result = probe(candidate)
    print(json.dumps(result, ensure_ascii=False))
    if select:
        save(candidate)
        print("已选用", name, "，运行 ./4s restart 生效")


def configure(action, name):
    saved = json.loads((STATE/"config.json").read_text())
    profiles = saved.setdefault("model_profiles", {})
    if not saved.get("active_profile") and model_ready(cfg):
        profiles.setdefault("legacy", {k: cfg[k] for k in MODEL_FIELDS})
    if action == "configure-callback":
        saved["callback_url"] = input("4S 接收回调的完整 HTTPS 地址 ").strip()
        saved["callback_token"] = getpass.getpass("4S 回调 Bearer 令牌（输入隐藏） ").strip()
        save(saved)
        print("回调配置已保存，运行 ./4s restart 生效")
    elif action == "model-list":
        print("已选择（重启时加载）", saved.get("active_profile") or ("legacy" if model_ready(cfg) else "off"))
        for alias, profile in sorted(profiles.items()):
            print(alias, profile["model"])
    elif action == "model-use" and name == "off":
        save(dict(saved, model="", model_base="", model_key="", active_profile="off"))
        print("已选择仅收件，运行 ./4s restart 生效")
    elif action == "configure-model":
        alias = name or "bailian"
        if alias == "off" or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", alias):
            raise ValueError("请使用简短的字母、数字、横线或下划线作为配置名称")
        defaults = {"deepseek": ("https://api.deepseek.com", "deepseek-flash"), "bailian": ("", "qwen3.7-plus")}
        base, model = defaults.get(alias, ("",""))
        old = profiles.get(alias, {})
        base, model = old.get("model_base", base), old.get("model", model)
        url = input(f"视觉接口 Base URL [{base}] ").strip() or base
        model_id = input(f"视觉模型 ID [{model}] ").strip() or model
        key = getpass.getpass("该端点的 API Key（输入隐藏） ").strip()
        profiles[alias] = dict(model=model_id, model_base=url, model_key=key)
        with_profile(saved, alias)
        save(saved)
        print("候选已保存", alias)
        if not name:
            check_and_select(saved, alias, True)
    else:
        check_and_select(saved, name, action == "model-use")


def main():
    parser = argparse.ArgumentParser(description="4S 报警复核试点")
    parser.add_argument("action", choices=["start","stop","restart","status","token","rotate-token","configure-model","configure-callback",
                                          "model-use","model-check","model-list","public-start","public-stop","public-status",
                                          "fixed-start","fixed-stop","fixed-restart","tunnel-setup","callbacks-retry"])
    parser.add_argument("value", nargs="?")
    args = parser.parse_args()
    if args.action=="token":
        if args.value not in ("live","admin"):
            parser.error("使用 ./4s token live 或 admin")
        print(admin_password(cfg) if args.value=="admin" else cfg[args.value+"_token"])
    elif args.action=="status":
        ok = bool(process()) and health()
        print("运行中" if ok else "未运行或未就绪", address)
        raise SystemExit(0 if ok else 1)
    else:
        with (STATE/"control.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if args.action=="rotate-token":
                if args.value != "live":
                    parser.error("使用 ./4s rotate-token live")
                if process():
                    parser.error("请先运行 ./4s stop，让网关和服务一起加载新令牌")
                saved = json.loads((STATE/"config.json").read_text())
                saved[args.value+"_token"] = secrets.token_urlsafe(32)
                save(saved)
                print("令牌已更新。运行 ./4s start 恢复服务。")
                return
            if args.action=="callbacks-retry":
                if args.value != "live":
                    parser.error("使用 ./4s callbacks-retry live")
                from app.config import callback_ready
                from app.store import db
                if not callback_ready(cfg,args.value):
                    parser.error("请先配置该环境的回调")
                with db(args.value) as c:
                    reset = c.execute("""UPDATE tasks SET callback_status='pending',callback_attempts=0,callback_due=0,
                                        callback_error=NULL WHERE historical_test=0 AND callback_status='failed'""").rowcount
                print("重新排队的回调", reset)
                return
            if args.action.startswith("fixed-"):
                from public_access import fixed_start, fixed_stop
                if args.action in ("fixed-stop", "fixed-restart"):
                    fixed_stop()
                if args.action != "fixed-stop":
                    start()
                    try:
                        ip = fixed_start(args.value)
                    except (ValueError, OSError, RuntimeError) as exc:
                        raise SystemExit(str(exc))
                    print("本机 HTTPS 网关已就绪，证书匹配", ip, "，监听端口", HTTPS_PORT)
                return
            if args.action.startswith("public-") or args.action == "tunnel-setup":
                from public_access import NGROK_CONFIG, configure_ngrok, public_start, public_status, public_stop
                if args.action in ("public-start", "tunnel-setup"):
                    start()
                    enrolling = args.action == "tunnel-setup" and not NGROK_CONFIG.exists()
                    if enrolling:
                        token = os.getenv("NGROK_AUTHTOKEN") or getpass.getpass("ngrok Authtoken（输入隐藏） ")
                        configure_ngrok(token.strip())
                    try:
                        print(public_start(cfg["port"]))
                    except BaseException:
                        if enrolling:
                            NGROK_CONFIG.unlink(missing_ok=True)
                        raise
                    (STATE / "public-paused").unlink(missing_ok=True)
                elif args.action == "public-stop":
                    public_stop()
                    if NGROK_CONFIG.is_file():
                        (STATE / "public-paused").touch()
                    print("公网入口已关闭")
                else:
                    print(public_status() or "公网入口未运行")
                return
            if args.action in ("stop","restart"):
                if args.action=="stop":
                    from public_access import fixed_stop, public_stop
                    public_stop()
                    fixed_stop()
                stop()
            if args.action in ("start","restart"):
                start()
                from public_access import NGROK_CONFIG, NGROK_URL, public_start
                if (NGROK_CONFIG.exists() or NGROK_URL.exists()) and not (STATE / "public-paused").exists():
                    print(public_start(cfg["port"]))
            if args.action.startswith(("configure-","model-")):
                try:
                    configure(args.action,args.value)
                except (ValueError, RemoteError) as exc:
                    raise SystemExit(str(exc))


if __name__=="__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as exc:
        raise SystemExit(str(exc))
