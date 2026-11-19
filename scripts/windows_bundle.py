"""Package a clean Windows installation without any machine state."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from review_bundle import MEMBERS

root = Path(__file__).resolve().parents[1]
target = root / "交付" / "4S-Windows.zip"
selected = [name for name in MEMBERS if name.startswith(("app/", "web/", "windows/"))]
selected.extend(("scripts/public_gateway.py", "requirements.txt", "examples/test-card.png",
                 "docs/4S-API.md", "docs/4S-API.pdf", "docs/4S-联调包.zip"))
target.parent.mkdir(exist_ok=True)
stage = target.with_suffix(".new")
with ZipFile(stage, "w", ZIP_DEFLATED) as archive:
    for name in selected:
        archive.write(root / name, name)
with ZipFile(stage) as archive:
    if archive.testzip():
        raise RuntimeError("Archive verification failed")
stage.replace(target)
print(target)
