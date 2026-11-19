"""Package reviewable sources and synthetic evidence using an explicit release manifest."""
import hashlib
import os
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEMBERS = (
    "README-审核入口.md", "README.md", "4s", "requirements.txt", ".gitignore",
    "app/__init__.py", "app/config.py", "app/contracts.py", "app/main.py",
    "app/model_profiles.py", "app/model_catalog.py", "app/review.py", "app/store.py", "app/worker.py",
    "web/admin.html", "web/admin.js", "web/style.css", "web/api-docs.html",
    "scripts/manage.py", "scripts/serve.py", "scripts/submit.py", "scripts/package.py", "scripts/public_gateway.py", "scripts/public_access.py",
    "scripts/review_bundle.py", "scripts/check.py", "tests/pilot.py", "tests/profile_switch.py", "tests/admin_models.py", "tests/legacy_migration.py", "tests/relocation.py", "tests/fixed_entry.py",
    "tests/tunnel_restart.py", "tests/total_timeout.py", "tests/model_fallback.py",
    "examples/model-profile.example.json", "examples/test-card.png",
    "scripts/render_api_pdf.py", "docs/项目需求与接口对照.md", "docs/4S-API.md", "docs/4S-API.pdf", "docs/4S-联调包.zip", "docs/openapi.json", "docs/admin-demo.jpg",
    "docs/review/01-背景与实施计划.md", "docs/review/02-换模型与迁移.md",
    "docs/review/03-验证记录.md", "docs/review/04-公网联调与审阅处理.md", "docs/review/runtime-mac.txt",
)


def build():
    dest = ROOT/"交付"/"4S-告警复核-Agent审核包.zip"
    dest.parent.mkdir(exist_ok=True)
    staged = dest.with_suffix(".new")
    sums = []
    with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED) as z:
        for name in MEMBERS:
            path = ROOT/name
            if path.resolve() != path or not path.is_file():
                raise ValueError("归档来源须为项目内的普通文件: "+name)
            z.write(path,name)
            sums.append(hashlib.sha256(z.read(name)).hexdigest()+"  "+name)
        z.writestr("SHA256SUMS","\n".join(sums)+"\n")
    with zipfile.ZipFile(staged) as z:
        if z.testzip() is not None:
            raise RuntimeError("归档完整性检查失败")
    os.replace(staged,dest)
    checksum = hashlib.sha256(dest.read_bytes()).hexdigest()
    dest.with_suffix(".zip.sha256").write_text(checksum+"  "+dest.name+"\n")
    print(dest)
    print(checksum)


if __name__ == "__main__":
    build()
