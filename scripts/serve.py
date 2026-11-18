import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.umask(0o077)
from app.config import STATE, init_config
import uvicorn

if __name__ == "__main__":
    config = init_config()
    os.environ["TMPDIR"] = str(STATE / "tmp")
    (STATE / "tmp").mkdir(exist_ok=True)
    uvicorn.run("app.main:api", host=os.getenv("FOUR_S_BIND", "0.0.0.0"), port=config["port"],
                workers=1, proxy_headers=False, access_log=False, limit_concurrency=48,
                timeout_keep_alive=5, timeout_graceful_shutdown=8)
