import base64
import binascii
import hashlib
import io
import json
import math
import warnings
from datetime import timezone
from typing import Annotated, Literal, Optional, Union

from PIL import Image, UnidentifiedImageError
from pydantic import AfterValidator, BeforeValidator, AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from .config import MAX_IMAGE

Alarm = Literal["no_helmet", "no_vest", "danger_zone", "combined"]
Verdict = Literal["valid", "false_alarm", "uncertain"]
Status = Literal["awaiting_model", "queued", "processing", "retrying", "completed", "failed"]
Delivery = Literal["waiting_result", "unconfigured", "pending", "sending", "retrying", "delivered", "failed"]
PPE = ("no_helmet", "no_vest")
RULE = "ppe-3"
REVIEW_DELAYS = (5, 30)
CALLBACK_DELAYS = (5, 30, 120, 600)
def local_review_model(mode, project_id, alarm_type):
    if alarm_type not in PPE:
        return "scope-rule"
    return None


class Health(BaseModel):
    status: Literal["ok"]


class FieldIssue(BaseModel):
    loc: list[Union[str, int]]
    msg: str


class RequestError(BaseModel):
    detail: list[FieldIssue]


def numeric_box(value):
    if not isinstance(value, list) or any(type(v) not in (int, float) for v in value):
        raise ValueError("坐标须为四个数值")
    return value


def ordered_box(value):
    left, top, right, bottom = value
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ValueError("坐标应为归一化的 [左,上,右,下]")
    return value


Box = Annotated[list[float], Field(min_length=4, max_length=4), BeforeValidator(numeric_box), AfterValidator(ordered_box)]
class AlarmFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=100, description="项目编号，1–100 个字符。")
    alert_id: str = Field(min_length=1, max_length=100)
    alarm_type: Alarm
    camera_id: Optional[str] = Field(default=None, max_length=100)
    occurred_at: Optional[AwareDatetime] = None

    @field_validator("project_id", "alert_id", "camera_id")
    @classmethod
    def clean_id(cls, value):
        if value is not None and (not value.strip() or value.strip() != value or not value.isprintable()):
            raise ValueError("编号含空白边界或不可见字符")
        return value

    @field_validator("occurred_at", mode="before")
    @classmethod
    def iso_string(cls, value):
        if value is not None and (not isinstance(value, str) or "T" not in value.upper()):
            raise ValueError("请使用带时区的 ISO 8601 时间字符串")
        return value


class FrameInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_base64: str = Field(min_length=4, max_length=4 * ((MAX_IMAGE + 2) // 3), description="JPEG/PNG，标准 Base64，无前缀")
    roi: Optional[Box] = None
    person_boxes: Optional[list[Box]] = None


class Submission(AlarmFields, FrameInput):
    image_id: Optional[str] = Field(default=None, min_length=1, max_length=100,
                                    description="旧版单图请求的图片编号。")

    _clean_image_id = field_validator("image_id")(AlarmFields.clean_id)


class AlarmSubmission(AlarmFields):
    images: list[FrameInput] = Field(min_length=1, max_length=5)


Intake = Union[AlarmSubmission, Submission]


class FrameResult(BaseModel):
    index: int
    status: Literal["pending", "retrying", "completed", "failed"]
    verdict: Optional[Verdict]
    reason: Optional[str]
    error: Optional[str]
    model: Optional[str]
    roi: Optional[Box]
    person_boxes: Optional[list[Box]]


class Event(BaseModel):
    received_at: str
    schema_version: Literal["1"] = "1"
    task_id: str
    project_id: str
    alert_id: str
    image_id: Optional[str] = None
    alarm_type: Alarm
    occurred_at: Optional[str]
    mode: Literal["live"]
    status: Status
    verdict: Optional[Verdict] = Field(description="必返字段。审核完成时为 valid、false_alarm 或 uncertain；未完成或技术失败时为 null。")
    reason: Optional[str]
    error: Optional[str]
    model: Optional[str]
    rule_version: str
    finished_at: Optional[str]
    images: list[FrameResult]


class CallbackEvent(Event):
    status: Literal["completed", "failed"]


class Task(Event):
    person_boxes: Optional[list[Box]] = None
    roi: Optional[list[float]]
    camera_id: Optional[str]
    started_at: Optional[str]
    elapsed_ms: Optional[int]
    review_attempts: int
    callback_attempts: int
    callback_status: Delivery
    callback_error: Optional[str]


def unpack(s):
    try:
        data = base64.b64decode(s.image_base64, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("image_base64 编码无效") from None
    if len(data) > MAX_IMAGE:
        raise OverflowError("图片超过 8 MiB")
    try:
        with warnings.catch_warnings(), Image.open(io.BytesIO(data)) as img:
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            fmt = img.format
            if fmt not in ("JPEG", "PNG") or math.prod(img.size) > 16_000_000 or getattr(img, "n_frames", 1) != 1:
                raise ValueError("只接收 1600 万像素以内的单张 JPEG/PNG")
            img.verify()
        with Image.open(io.BytesIO(data)) as img:
            img.load()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("图片损坏或尺寸过大") from exc
    meta = s.model_dump(exclude={"image_base64","occurred_at"})
    meta["occurred_at"] = s.occurred_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if s.occurred_at else None
    if meta["image_id"] is None:
        del meta["image_id"]  # Legacy single-image requests retain their signature.
    if meta["person_boxes"] is None:
        del meta["person_boxes"]  # Preserve signatures of submissions made before boxes were supported.
    signature = hashlib.sha256(json.dumps(meta, sort_keys=True).encode() + hashlib.sha256(data).digest()).hexdigest()
    return meta, data, "image/" + fmt.lower(), signature


def unpack_alarm(submission):
    if isinstance(submission, Submission):
        return (*unpack(submission), [])
    common = submission.model_dump(exclude={"images"}, mode="json")
    frames = []
    for index, frame in enumerate(submission.images, 1):
        try:
            frames.append(unpack(Submission(**common, **frame.model_dump())))
        except ValueError as exc:
            raise ValueError(f"第 {index} 张图片：{exc}") from exc
    meta, data, mime, signature = frames[0]
    if len(frames) > 1:
        signature = hashlib.sha256(json.dumps([f[3] for f in frames]).encode()).hexdigest()
    return meta, data, mime, signature, frames[1:]
