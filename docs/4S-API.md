# 4S 报警图片复核 API v1

| 版本 | 日期 | 说明 |
|---|---|---|
| 1.2 | 2026-11 | 一条报警一次提交 1–5 张图，返回一个任务号和一个总结果 |

## 1 通用约定

| 项目 | 约定 |
|---|---|
| 根地址 | `https://<host>`，由部署方提供。下文路径均相对于根地址 |
| 鉴权 | 请求头 `Authorization: Bearer <接口令牌>`。令牌由部署方单独提供，请勿写入代码仓库或日志 |
| 数据格式 | `Content-Type: application/json`，UTF-8 |
| 时间格式 | ISO 8601，带时区，例如 `2026-11-18T09:00:00+08:00`。响应中的时间统一为 UTC，例如 `2026-11-18T01:00:00Z` |
| 字段兼容 | 后续版本可能在响应中新增字段，调用方请忽略不认识的字段 |
| 空值 | 类型写作 `string / null` 的字段始终存在于响应中，没有值时为 JSON 的 `null` |

调用流程：

1. 将一条报警的 1–5 张图片一次提交。全部保存后返回一个 `task_id`。
2. 服务逐张复核并汇总。4S 通过回调或查询取得报警级 `status` 和 `verdict`。
3. 4S 自行决定报警的展示、通知和过滤。

## 2 提交报警

`POST /api/v1/tasks`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `project_id` | string | 是 | 项目编号，1–100 个字符 |
| `alert_id` | string | 是 | 报警编号，1–100 个字符，项目内唯一 |
| `alarm_type` | string | 是 | 报警类型，取值见第 6 节 |
| `images` | array | 是 | 1–5 张，按原顺序提交 |
| `camera_id` | string | 否 | 摄像头编号，最多 100 个字符 |
| `occurred_at` | string | 否 | 带时区的报警发生时间 |

每个 images 元素的字段如下。每张原图限 8 MiB、1600 万像素，整个 JSON 请求限 56 MiB。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `image_base64` | string | 是 | JPEG 或 PNG 的标准 Base64，不带前缀、不换行 |
| `roi` | number[] | 否 | 本图复核区域 [左, 上, 右, 下]，归一化至 0–1。省略时审核整图 |
| `person_boxes` | number[][] | 否 | 本图候选人员框，坐标相对本图，格式同 roi，仅供定位 |

```json
{
  "project_id": "site-01", "alert_id": "A-0001", "alarm_type": "no_helmet",
  "images": [
    { "image_base64": "<第一张>", "roi": [0.1, 0, 0.9, 1] },
    { "image_base64": "<第二张>" }
  ]
}
```

| HTTP | 含义 |
|---|---|
| 202 | 全部原图与任务已保存，返回一个任务对象 |
| 200 | 原样重传，返回原任务号 |
| 409 | 同项目同报警编号已存在，图片、顺序或字段有变化 |

重传时保持请求内容及图片顺序一致。一张无效则整单拒收。旧版单图请求仍可提交或原样重传，历史任务仍可按原任务号查询。

## 3 查询复核结果

`GET /api/v1/tasks/{task_id}`。成功返回 HTTP 200。

| 字段 | 类型 | 说明 |
|---|---|---|
| `task_id` | string | 提交时返回的任务号 |
| `project_id`、`alert_id` | string | 项目编号、报警编号 |
| `alarm_type` | string | 报警类型 |
| `status` | string | 整条报警的处理状态，见第 6 节 |
| `verdict` | string / null | 整条报警的结论，处理中或全部技术失败时为 null |
| `reason`、`error` | string / null | 判断理由、技术失败说明 |
| `images` | array | 按提交顺序返回逐图明细。index 从 1 开始；含 status、verdict、reason、error、model、roi 和 person_boxes |
| `received_at`、`finished_at` | string / null | 收件时间、处理完成时间 |

完成响应节选：

```json
{
  "task_id": "0123456789abcdef0123456789abcdef",
  "alert_id": "A-0001", "status": "completed", "verdict": "valid",
  "reason": "第 2 张：区域内一人未戴安全帽。",
  "images": [
    { "index": 1, "status": "completed", "verdict": "uncertain" },
    { "index": 2, "status": "completed", "verdict": "valid" }
  ]
}
```

| 图片结果（按优先级） | 报警结果 |
|---|---|
| 任意一张有效 | completed / valid |
| 全部明确误报 | completed / false_alarm |
| 全部技术失败 | failed / null |
| 其余组合，包括部分失败或证据不足 | completed / uncertain |

## 4 结果回调

复核结束时（`status` 为 `completed` 或 `failed`），每条报警产生一个结果回调，携带任务号、报警总结果和逐图明细。

| 项目 | 约定 |
|---|---|
| 地址 | 各项目默认使用 4S 提供的 HTTPS 回调地址 |
| 请求头 | `Content-Type: application/json`。当前接收端免鉴权；启用鉴权时使用 4S 单独提供的回调令牌 |
| 请求体 | 任务对象，字段同第 3 节，不含图片 |
| 4S 响应 | 保存结果后返回 HTTP 2xx，JSON 包含 `"success": true` |
| 重复送达 | 同一结果可能送达多次。请按 `task_id` 去重，重复送达时返回相同的成功回执 |
| 重试 | 单次超时 10 秒。失败后间隔 5、30、120、600 秒重试，最多发送 5 次 |

回调未配置或重试用完后，结果仍可通过第 3 节查询。历史结果按约定的项目范围补发。

## 5 服务状态

`GET /healthz`，无需鉴权。

| HTTP | 含义 |
|---|---|
| 200 | 服务正常，返回 `{"status":"ok"}` |
| 503 | 服务暂不可用 |

## 6 数据字典

### alarm_type 报警类型

| 值 | 说明 |
|---|---|
| `no_helmet` | 未戴安全帽 |
| `no_vest` | 未穿反光衣 |
| `danger_zone` | 危险区域，当前版本固定返回 `uncertain` |
| `combined` | 组合报警，当前版本固定返回 `uncertain` |

### status 处理状态

| 值 | 说明 | 是否已结束 |
|---|---|---|
| `awaiting_model` | 已收图，等待启用模型；启用后继续审核 | 否 |
| `queued` | 排队中 | 否 |
| `processing` | 复核中 | 否 |
| `retrying` | 上次尝试失败，等待重试 | 否 |
| `completed` | 复核完成，`verdict` 有值 | 是 |
| `failed` | 技术失败，`verdict` 为 `null` | 是 |

### verdict 复核结论

| 值 | 说明 |
|---|---|
| `valid` | 有效报警：区域内有人明确存在本条类型的违规 |
| `false_alarm` | 误报：画面清楚，可以排除区域内所有人的本条违规 |
| `uncertain` | 无法确定：无人、非现场画面、模糊、遮挡或证据不足 |
| `null` | 暂无结论：尚未结束，或技术失败 |

## 7 错误码

错误响应体为 `{"detail": ...}`。

| HTTP | 含义 | 处理 |
|---|---|---|
| 401 | 令牌缺失或无效 | 检查 `Authorization` 请求头 |
| 404 | 接口或任务不存在 | 核对路径和 `task_id` |
| 409 | 同一报警编号内容冲突 | 核对首次提交的图片和字段 |
| 413 | 请求超出大小限制 | 检查图片和请求体大小 |
| 415 | 请求格式错误 | 设置 `Content-Type: application/json` |
| 422 | 字段、图片或区域无效 | 按 `detail` 修正，`detail` 为列表，每项含 `loc` 和 `msg` |
| 429 / 5xx | 限流或服务异常 | 稍后用原请求重传 |

## 8 处理时间

- 单张图片的复核通常在数秒内完成。
- 逐张处理，仅对技术失败的图片重试，最多 3 轮。若启用了备用模型，每轮可能依次尝试主模型和备用模型；主模型超时由后台设置，备用模型单次超时 60 秒。全部失败时返回 `failed`，结论为 `null`。处理时间还取决于排队和重试等待。
- 建议 4S 为每条报警设定自己的等待上限，超时后按自身规则处理。

