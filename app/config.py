import json
import os
import plistlib
import secrets
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DISK = Path(os.environ.get("FOUR_S_VOLUME", "/Volumes/Data")).resolve()
STATE = Path(os.environ.get("FOUR_S_STATE", ROOT / "var")).resolve()
MB = 1024 * 1024
MAX_IMAGE = 8 * MB
MAX_BODY = 56 * MB
HTTPS_BIND = os.getenv("FOUR_S_HTTPS_BIND", "0.0.0.0")
HTTPS_PORT = int(os.getenv("FOUR_S_HTTPS_PORT", "8443"))
MODEL_NAME = "deepseek-flash"
CONFIG_LOCK = threading.RLock()


def acquire_file_lock(handle, blocking=True):
    # Closing the handle releases the lock on both platforms.
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(handle, flags)


def check_disk():
    if DISK == Path("/") or not os.path.ismount(DISK) or DISK not in ROOT.parents or ROOT not in STATE.parents:
        raise RuntimeError("项目和状态目录必须位于已挂载的专用数据卷")
    if sys.platform == "darwin":
        info = plistlib.loads(subprocess.check_output(["/usr/sbin/diskutil", "info", "-plist", str(DISK)]))
        if info.get("Internal") is not False or info.get("MountPoint") != str(DISK):
            raise RuntimeError("Mac 试点数据目录须位于外置卷")
    elif os.name == "nt":
        from ctypes import windll
        if (not os.environ.get("FOUR_S_VOLUME") or DISK != Path(DISK.anchor)
                or DISK.drive.casefold() == os.environ["SystemDrive"].casefold()
                or windll.kernel32.GetDriveTypeW(str(DISK)) != 3):
            raise RuntimeError("Windows 部署须指定独立的本地固定数据盘，如 D 盘")
    elif not sys.platform.startswith("linux"):
        raise RuntimeError("目前支持 macOS 及使用专用挂载卷的 Linux / Windows 部署")
    if ROOT.stat().st_dev != DISK.stat().st_dev:
        raise RuntimeError("数据目录必须位于 Data 卷内")


def init_config():
    check_disk()
    os.umask(0o077)
    STATE.mkdir(exist_ok=True, mode=0o700, parents=True)
    if STATE.stat().st_dev != DISK.stat().st_dev:
        raise RuntimeError("状态目录不在 Data 卷内")
    config_path = STATE / "config.json"
    if not config_path.exists():
        config = {"live_token": secrets.token_urlsafe(32)}
        config.update(port=8765, active_profile="off", model=MODEL_NAME,
                      model_base="", model_key="", callback_url="", callback_token="")
        with config_path.open("x") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    config = validate_config(json.loads(config_path.read_text(encoding="utf-8")))
    with (STATE/"admin.secret").open("a+") as secret:
        acquire_file_lock(secret)
        secret.seek(0)
        password = secret.read().strip()
        if not password:
            password = secrets.token_urlsafe(32)
            secret.write(password+"\n")
            secret.flush()
        config["admin_password"] = password
    return config


def validate_config(saved):
    config = dict(saved)
    # A selected profile owns its endpoint and credential together.
    # Only the original Bailian setup may inherit DashScope environment settings.
    if not config.get("active_profile"):
        config["model_base"] = config["model_base"] or os.getenv("DASHSCOPE_BASE_URL", "")
        host = urlsplit(config["model_base"]).hostname or ""
        if host.endswith(".aliyuncs.com"):
            config["model_key"] = config["model_key"] or os.getenv("DASHSCOPE_API_KEY", "") or os.getenv("BAILIAN_API_KEY", "")
    for key in ("model_base", "callback_url"):
        url = config[key]
        if url:
            parsed = urlsplit(url)
            local = parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
            if (parsed.scheme != "https" and not local) or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise ValueError(f"{key} 请填 HTTPS 地址（本机可用 HTTP）")
    if not isinstance(config["live_token"], str) or len(config["live_token"]) < 24:
        raise ValueError("接口令牌太短")
    scope = config.get("callback_test_project")
    if scope is not None and (not isinstance(scope, str) or not scope.strip() or scope != scope.strip() or len(scope) > 100):
        raise ValueError("请填写有效的回调测试项目编号")
    return config


def save_config(saved):
    validate_config(saved)
    fd, filename = tempfile.mkstemp(prefix=".config-", dir=STATE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(saved, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(filename, STATE / "config.json")
    finally:
        if os.path.exists(filename):
            os.unlink(filename)


def model_ready(config):
    return bool(config["model_key"] and config["model_base"] and config["model"])


def admin_password(config):
    return config["admin_password"]


def callback_ready(config, mode, project_id=None):
    scope = config.get("callback_test_project")
    return bool(config["callback_url"] and (config["callback_token"] or scope)
                and (scope is None or project_id is None or project_id == scope))
