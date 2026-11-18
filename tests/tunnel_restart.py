"""Exercise tunnel lifecycle offline, using a local stand-in for the ngrok process."""
import json
import os
import subprocess
import tempfile
from pathlib import Path
from urllib import request

from pilot import ROOT, unused_port


def run(state):
    binary = state / "bin/ngrok"
    binary.parent.mkdir()
    binary.write_text("""#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
if sys.argv[1:3] == ["config", "check"]:
    sys.exit(0)
config = Path(sys.argv[sys.argv.index("--config") + 1])
assert "NGROK_AUTHTOKEN" not in os.environ
assert "--inspect=false" in sys.argv
given = sys.argv[sys.argv.index("--url") + 1]
(config.parent / "requests").open("a").write(given + "\\n")
if json.loads(config.read_text())["agent"]["authtoken"] == "rejected":
    print('ERR_NGROK_105', flush=True)
    sys.exit(1)
domain = "https://contract-test.ngrok-free.dev"
if (config.parent / "mismatch").exists():
    domain = "https://unexpected.ngrok-free.dev"
print(json.dumps({"msg":"started tunnel", "url":domain}), flush=True)
time.sleep(300)
""")
    binary.chmod(0o700)
    env = dict(os.environ, FOUR_S_STATE=str(state), FOUR_S_BIND="127.0.0.1")
    def manage(*args, check=True, token=None):
        settings = dict(env)
        if token:
            settings["NGROK_AUTHTOKEN"] = token
        return subprocess.run([str(ROOT / "4s"), *args], env=settings, check=check,
                              capture_output=True, text=True, timeout=35)
    manage("model-list")
    cfg = state / "config.json"
    port = unused_port()
    cfg.write_text(json.dumps(dict(json.loads(cfg.read_text()), port=port)))
    client = request.build_opener(request.ProxyHandler({}))
    domain = "https://contract-test.ngrok-free.dev"
    try:
        assert manage("tunnel-setup", check=False, token="rejected").returncode != 0
        assert not (state / "ngrok.yml").exists()
        assert domain in manage("tunnel-setup", token="offline-fixture").stdout
        assert (state / "ngrok.yml").stat().st_mode & 0o077 == 0
        with client.open(f"http://127.0.0.1:{port+1}/healthz", timeout=2) as response:
            assert json.load(response) == {"status": "ok"}
        manage("stop")
        assert domain in manage("start").stdout
        assert manage("public-status").stdout.strip() == domain
        assert (state / "requests").read_text().splitlines()[-2:] == ["https://", domain]
        manage("public-stop")
        manage("restart")
        assert domain not in manage("public-status").stdout
        (state / "mismatch").touch()
        assert manage("public-start", check=False).returncode != 0
        assert (state / "ngrok-url.txt").read_text().strip() == domain
        (state / "mismatch").unlink()
        assert domain in manage("public-start").stdout
        assert not (state / "public-paused").exists()
        print("PASS offline ngrok enrollment, repeat URL after full restart, manual pause and mismatch refusal")
    finally:
        manage("stop")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(dir=ROOT / "var", prefix="ngrok-qa-") as directory:
        run(Path(directory))
