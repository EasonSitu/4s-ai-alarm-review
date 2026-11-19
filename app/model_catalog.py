"""Read provider catalogs without exposing credentials to the browser."""
import ssl
import time
from urllib import error, request
from urllib.parse import urlencode, urlsplit, urlunsplit

import certifi

from .review import NoRedirect, RemoteError, read_json_response


def valid_base(base):
    parsed = urlsplit(base)
    local = parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if ((parsed.scheme != "https" and not local) or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("API 基础地址请使用 HTTPS，且不要包含账号、查询参数或片段")
    return parsed


def read_catalog(url, key):
    req = request.Request(url, headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
    handlers = [NoRedirect(), request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where()))]
    if urlsplit(url).hostname in ("localhost", "127.0.0.1", "::1"):
        handlers.append(request.ProxyHandler({}))
    try:
        deadline = time.monotonic() + 15
        with request.build_opener(*handlers).open(req, timeout=15) as response:
            return read_json_response(response, deadline, 2 * 1024 * 1024)
    except error.HTTPError as exc:
        exc.close()
        raise RemoteError(f"获取模型列表失败：HTTP {exc.code}") from None
    except (OSError, ValueError) as exc:
        raise RemoteError("获取模型列表失败 · " + type(exc).__name__) from None


def list_models(provider, base, key):
    parsed = valid_base(base)
    if not key:
        raise ValueError("请输入该平台的 API Key")
    rows, truncated = [], False
    if provider == "bailian":
        for page in range(1, 6):
            query = urlencode({"capabilities": "VU", "page_no": page, "page_size": 100})
            url = urlunsplit((parsed.scheme, parsed.netloc, "/api/v1/models", query, ""))
            data = read_catalog(url, key)
            output = data.get("output") if isinstance(data, dict) else None
            if not isinstance(output, dict) or not isinstance(output.get("models"), list):
                raise RemoteError("百炼模型列表格式不正确")
            page_rows = output["models"]
            rows.extend(page_rows)
            total = output.get("total")
            if not page_rows or (isinstance(total, int) and len(rows) >= total) or (not isinstance(total, int) and len(page_rows) < 100):
                break
        else:
            truncated = True
    else:
        data = read_catalog(base.rstrip("/") + "/models", key)
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise RemoteError("模型列表格式不正确")
    models, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        model = row.get("model") if provider == "bailian" else row.get("id")
        if not isinstance(model, str) or not model or len(model) > 160 or any(ord(c) < 32 for c in model) or model in seen:
            continue
        if provider == "bailian":
            meta = row.get("inference_metadata") or {}
            modalities = meta.get("request_modality") if isinstance(meta, dict) else None
        else:
            modalities = row.get("input_modalities")
        if isinstance(modalities, list):
            if "image" not in [str(item).lower() for item in modalities]:
                continue
            vision = "supported"
        else:
            vision = "unknown"
        seen.add(model)
        models.append({"id": model, "vision": vision})
    return {"models": models, "truncated": truncated}
