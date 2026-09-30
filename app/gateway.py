"""课程网关适配层（design.md §4.1）

规约依据 `add-traceable-vector-retrieval`：
  - 嵌入请求体**只含** `model` 与 `input`（课件接口契约）
  - `data[0].embedding` 长度 MUST 等于 `EMBEDDING_DIM`，否则判为失败且不写入向量
  - 对话仅在**有命中切片**时调用（D12）
  - **密钥只在服务端**：只从环境变量读取，绝不下发浏览器

零第三方依赖：只用标准库 urllib。传输层可注入（`transport=`），便于测试与离线验收。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_EMBED_TIMEOUT = 15
DEFAULT_CHAT_TIMEOUT = 30
BATCH_SIZE = 64


class GatewayError(RuntimeError):
    """网关调用失败。子类携带对外错误码。"""

    code = "upstream_error"


class EmbeddingUnavailable(GatewayError):
    code = "embedding_unavailable"


class ChatUnavailable(GatewayError):
    code = "upstream_error"


class EmbeddingDimMismatch(GatewayError):
    """返回维度与 EMBEDDING_DIM 不一致 —— 按失败处理，不写入向量。"""

    code = "embedding_dim_mismatch"


@dataclass
class GatewayConfig:
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_dim: int = 0
    chat_base_url: str = ""
    chat_api_key: str = ""
    chat_model: str = ""
    embed_timeout: int = DEFAULT_EMBED_TIMEOUT
    chat_timeout: int = DEFAULT_CHAT_TIMEOUT

    @property
    def embedding_ready(self) -> bool:
        return bool(
            self.embedding_base_url and self.embedding_api_key and self.embedding_model
            and self.embedding_dim > 0
        )

    @property
    def chat_ready(self) -> bool:
        return bool(self.chat_base_url and self.chat_api_key and self.chat_model)

    def redacted(self) -> dict:
        """可安全输出的配置视图（**不含密钥**）。"""
        return {
            "embedding_base_url": self.embedding_base_url,
            "embedding_model": self.embedding_model,
            "embedding_dim": self.embedding_dim,
            "embedding_ready": self.embedding_ready,
            "chat_model": self.chat_model,
            "chat_ready": self.chat_ready,
        }

    @classmethod
    def from_app(cls, app=None) -> "GatewayConfig":
        def env(name: str, default: str = "") -> str:
            if app is not None:
                cfg_key = name
                if cfg_key in app.config and app.config[cfg_key]:
                    return str(app.config[cfg_key])
            return str(os.environ.get(name, default) or "")

        def env_int(name: str, default: int) -> int:
            raw = env(name, "")
            try:
                return int(raw) if str(raw).strip() else default
            except (TypeError, ValueError):
                return default

        base = env("EMBEDDING_BASE_URL").rstrip("/")
        key = env("EMBEDDING_API_KEY")
        return cls(
            embedding_base_url=base,
            embedding_api_key=key,
            embedding_model=env("EMBEDDING_MODEL"),
            embedding_dim=env_int("EMBEDDING_DIM", 0),
            chat_base_url=(env("CHAT_BASE_URL") or base).rstrip("/"),
            chat_api_key=env("CHAT_API_KEY") or key,
            chat_model=env("CHAT_MODEL"),
            embed_timeout=env_int("EMBEDDING_TIMEOUT", DEFAULT_EMBED_TIMEOUT),
            chat_timeout=env_int("CHAT_TIMEOUT", DEFAULT_CHAT_TIMEOUT),
        )


# --------------------------------------------------------------------- 默认传输


def http_json(url: str, headers: dict, payload: dict, timeout: int) -> dict:
    """唯一真正发出网络请求的地方。测试通过注入 transport 完全绕开它。"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # noqa: PERF203
        raise GatewayError(f"网关返回 HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise GatewayError(f"网关不可达：{type(exc).__name__}") from exc
    try:
        return json.loads(body or "{}")
    except json.JSONDecodeError as exc:
        raise GatewayError("网关返回非 JSON 响应") from exc


def _auth_headers(cfg_key: str) -> dict:
    return {"Authorization": f"Bearer {cfg_key}"} if cfg_key else {}


# --------------------------------------------------------------------- 嵌入


def embed(texts: list[str], *, cfg: GatewayConfig, transport=None) -> list[list[float]]:
    """批量嵌入。请求体只含 `model` 与 `input`。

    维度不一致 → `EmbeddingDimMismatch`（调用方据此把切片标记为 failed，**不写入向量**）。
    """
    if not cfg.embedding_ready:
        raise EmbeddingUnavailable("嵌入网关未配置（缺少 EMBEDDING_* 环境变量）")
    if not texts:
        return []

    send = transport or http_json
    url = f"{cfg.embedding_base_url}/embeddings"
    headers = _auth_headers(cfg.embedding_api_key)
    vectors: list[list[float]] = []

    for start in range(0, len(texts), BATCH_SIZE):
        batch = texts[start:start + BATCH_SIZE]
        payload = {"model": cfg.embedding_model, "input": batch}   # 只提交 model 与 input
        try:
            resp = send(url, headers, payload, cfg.embed_timeout)
        except GatewayError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingUnavailable(f"嵌入调用失败：{type(exc).__name__}") from exc

        data = (resp or {}).get("data") or []
        if len(data) != len(batch):
            raise EmbeddingUnavailable("嵌入返回条数与输入不一致")
        for item in data:
            vec = item.get("embedding")
            if not isinstance(vec, list) or len(vec) != cfg.embedding_dim:
                raise EmbeddingDimMismatch(
                    f"嵌入维度不是 EMBEDDING_DIM({cfg.embedding_dim})"
                )
            vectors.append([float(x) for x in vec])
    return vectors


# --------------------------------------------------------------------- 对话


def chat(messages: list[dict], *, cfg: GatewayConfig, transport=None, temperature: float = 0.0) -> str:
    """一次性简短回答（D14）。失败抛 ChatUnavailable，调用方据此返回 502，**不编造答案**。"""
    if not cfg.chat_ready:
        raise ChatUnavailable("对话网关未配置（缺少 CHAT_MODEL 等环境变量）")

    send = transport or http_json
    url = f"{cfg.chat_base_url}/chat/completions"
    payload = {"model": cfg.chat_model, "messages": messages, "temperature": temperature}
    try:
        resp = send(url, _auth_headers(cfg.chat_api_key), payload, cfg.chat_timeout)
    except GatewayError as exc:
        raise ChatUnavailable(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise ChatUnavailable(f"对话调用失败：{type(exc).__name__}") from exc

    choices = (resp or {}).get("choices") or []
    if not choices:
        raise ChatUnavailable("对话网关未返回 choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not isinstance(content, str):
        raise ChatUnavailable("对话网关未返回文本内容")
    return content
