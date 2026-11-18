"""Build the handoff kit from the running application's API definitions. Never include var/."""
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.main import api, guide_text

if __name__ == "__main__":
    target = ROOT/"docs/4S-联调包.zip"
    stage = target.with_suffix(".new")
    schema = json.dumps(api.openapi(),ensure_ascii=False,indent=2)+"\n"
    with zipfile.ZipFile(stage, "w", zipfile.ZIP_DEFLATED) as kit:
        kit.writestr("4S-API.md", guide_text())
        kit.write(ROOT/"docs/4S-API.pdf","4S-API.pdf")
        kit.writestr("openapi.json", schema)
        for path in ("scripts/submit.py","scripts/check.py"):
            kit.write(ROOT/path,path)
    stage.replace(target)
    (ROOT/"docs/openapi.json").write_text(schema)
    print(target)
