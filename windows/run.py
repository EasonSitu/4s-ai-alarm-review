"""Windows scheduled-task entry point. Keep both listeners on loopback."""
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
state = root / "var"
mode = sys.argv[1]
if mode not in ("api", "gateway"):
    raise SystemExit("Expected api or gateway")
os.environ.update(FOUR_S_VOLUME=root.anchor, FOUR_S_STATE=str(state),
                  FOUR_S_BIND="127.0.0.1", PYTHONUTF8="1")
os.chdir(root)
(state / "tmp").mkdir(exist_ok=True)
for name in ("TMP", "TEMP", "TMPDIR"):
    os.environ[name] = str(state / "tmp")
from logging.handlers import RotatingFileHandler
import logging
import uvicorn
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "scripts"))
from app.config import init_config

if __name__ == "__main__":
    handler = RotatingFileHandler(state / (mode + ".log"), maxBytes=2_000_000,
                                  backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        config = init_config()
        settings = dict(host="127.0.0.1", proxy_headers=False, access_log=False,
                        log_config=None, timeout_keep_alive=5)
        if mode == "api":
            uvicorn.run("app.main:api", port=config["port"], limit_concurrency=48,
                        timeout_graceful_shutdown=8, **settings)
        else:
            from public_gateway import cfg, gateway
            cfg.update(config)
            uvicorn.run(gateway, port=config["port"] + 1, lifespan="off", ws="none",
                        limit_concurrency=20, **settings)
    except Exception:
        logger.exception("Startup failed")
        raise
