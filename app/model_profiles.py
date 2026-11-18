"""Operator-selected vision profiles. Probe with generated images before switching."""
import io
import json
import secrets
from time import monotonic

from PIL import Image, ImageDraw, ImageFont

from .config import ROOT, model_ready, validate_config
from .review import RemoteError, image_completion, review

MODEL_FIELDS = ("model", "model_base", "model_key")


def with_profile(saved, name):
    profiles = saved.get("model_profiles", {})
    if name not in profiles:
        raise ValueError("找不到模型配置，请先运行 configure-model")
    candidate = dict(saved, active_profile=name)
    candidate.update({k: profiles[name][k] for k in MODEL_FIELDS})
    checked = validate_config(candidate)
    if not model_ready(checked):
        raise ValueError("模型地址、型号和密钥需要填齐")
    return candidate


def probe(candidate):
    # Only the pixels carry the answer. A text-only endpoint must not pass.
    digits = f"{secrets.randbelow(1_000_000):06d}"
    image = Image.new("RGB", (500, 160), "white")
    ImageDraw.Draw(image).text((250, 80), digits, anchor="mm", fill="black", font=ImageFont.load_default(size=90))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    start = monotonic()
    content, _ = image_completion(candidate, "image/png", buffer.getvalue(),
                                  '读取图片里的六位数字，保留开头的零，仅返回 JSON 对象，字段 digits 为字符串。')
    try:
        matched = json.loads(content) == {"digits": digits}
    except ValueError:
        matched = False
    if not matched:
        raise RemoteError("未能读出合成图片上的随机数字")
    task = {"alarm_type": "no_helmet", "project_id": "model-probe", "roi": None}
    verdict, _, actual = review("live", candidate, task, "image/png", (ROOT/"examples/test-card.png").read_bytes())
    if verdict != "uncertain":
        raise RemoteError("模型未将非现场测试卡判为无法确定")
    return {"requested_model": candidate["model"], "returned_model": actual,
            "image_readback": True, "ppe_schema_valid": True, "synthetic_verdict": verdict,
            "two_calls_elapsed_ms": round((monotonic()-start)*1000)}
