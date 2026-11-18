"""Isolated profile lifecycle checks. All model responses here are simulated."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    with tempfile.TemporaryDirectory(dir=ROOT/"var", prefix="profile-qa-") as folder:
        os.environ["FOUR_S_STATE"] = folder
        from app.review import RemoteError
        from scripts import manage
        assert manage.STATE == Path(folder)
        config_path = Path(folder)/"config.json"
        saved = json.loads(config_path.read_text())
        saved["model_profiles"] = {
            alias: dict(model=alias+"-vision", model_base=f"http://127.0.0.1:{port}/v1", model_key=alias+"-test")
            for alias, port in (("first",8551), ("second",8552))}
        manage.save(saved)
        with patch.object(manage, "probe", return_value={"offline_fixture":True}):
            for name in ("first","second","first"):
                manage.configure("model-use", name)
                cfg = manage.init_config()
                assert cfg["active_profile"] == name
                assert {k:cfg[k] for k in manage.MODEL_FIELDS} == saved["model_profiles"][name]
        before = config_path.read_bytes()
        with patch.object(manage, "probe", side_effect=RemoteError("offline fixture")):
            try:
                manage.configure("model-use","second")
            except RemoteError:
                pass
            else:
                raise AssertionError("failed probe should leave the selected profile intact")
        assert config_path.read_bytes()==before
        manage.configure("model-use","off")
        with patch.dict(os.environ, {"DASHSCOPE_BASE_URL":"https://example.aliyuncs.com/v1","DASHSCOPE_API_KEY":"unused"}):
            assert not manage.model_ready(manage.init_config())
        assert set(json.loads(config_path.read_text())["model_profiles"])=={"first","second"}
        print("PASS switch A/B/A, restore endpoint/model/key, failed probe, off, legacy environment isolation")


if __name__=="__main__":
    main()
