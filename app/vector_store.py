"""Qdrant 向量库客户端（design.md §2.3 / §4.2）

规约依据 `add-traceable-vector-retrieval`：
  - 集合默认 `campusclaw_chunks`，距离 **Cosine**，size = `EMBEDDING_DIM`（D2）
  - point id = `knowledge_chunks.id`（D3）
  - payload 只含标识：`chunk_id` / `material_id` / `class_id` / `chunk_index` / `chunk_sha256`
    —— **不含正文**（D4）
  - 检索 MUST 带 `class_id` 作为 **pre-filter**（§5.2）
  - Qdrant 不可用时抛 `VectorStoreUnavailable`，调用方据此返回 503 且**不编造相似度**

零第三方依赖：只用标准库 urllib。传输层可注入（`transport=`）。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_COLLECTION = "campusclaw_chunks"
DEFAULT_TIMEOUT = 20


class VectorStoreUnavailable(RuntimeError):
    code = "vector_store_unavailable"


@dataclass
class QdrantConfig:
    url: str = ""
    collection: str = DEFAULT_COLLECTION
    api_key: str = ""
    dim: int = 0
    timeout: int = DEFAULT_TIMEOUT

    @property
    def ready(self) -> bool:
        return bool(self.url and self.collection and self.dim > 0)

    def redacted(self) -> dict:
        """可安全输出的配置视图（**不含密钥**）。"""
        return {
            "url": self.url,
            "collection": self.collection,
            "dim": self.dim,
            "ready": self.ready,
        }

    @classmethod
    def from_app(cls, app=None) -> "QdrantConfig":
        def env(name: str, default: str = "") -> str:
            if app is not None and app.config.get(name):
                return str(app.config[name])
            return str(os.environ.get(name, default) or "")

        def env_int(name: str, default: int) -> int:
            raw = env(name, "")
            try:
                return int(raw) if str(raw).strip() else default
            except (TypeError, ValueError):
                return default

        return cls(
            url=env("QDRANT_URL").rstrip("/"),
            collection=env("QDRANT_COLLECTION") or DEFAULT_COLLECTION,
            api_key=env("QDRANT_API_KEY"),
            dim=env_int("EMBEDDING_DIM", 0),
            timeout=env_int("QDRANT_TIMEOUT", DEFAULT_TIMEOUT),
        )


def _default_transport(method: str, url: str, headers: dict, payload: dict | None, timeout: int) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise VectorStoreUnavailable(f"Qdrant 返回 HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise VectorStoreUnavailable(f"Qdrant 不可达：{type(exc).__name__}") from exc
    try:
        return json.loads(body or "{}")
    except json.JSONDecodeError as exc:
        raise VectorStoreUnavailable("Qdrant 返回非 JSON 响应") from exc


class QdrantClient:
    """Qdrant 最小子集封装。所有方法在不可用时抛 `VectorStoreUnavailable`。"""

    def __init__(self, config: QdrantConfig, transport=None):
        self.config = config
        self._transport = transport or _default_transport

    # ------------------------------------------------------------- 内部

    @property
    def _headers(self) -> dict:
        return {"api-key": self.config.api_key} if self.config.api_key else {}

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        if not self.config.ready:
            raise VectorStoreUnavailable("向量库未配置（缺少 QDRANT_URL / EMBEDDING_DIM）")
        url = f"{self.config.url}{path}"
        try:
            return self._transport(method, url, self._headers, payload, self.config.timeout) or {}
        except VectorStoreUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreUnavailable(f"Qdrant 调用失败：{type(exc).__name__}") from exc

    def _collection_path(self, suffix: str = "") -> str:
        return f"/collections/{self.config.collection}{suffix}"

    # ------------------------------------------------------------- 集合

    def available(self) -> bool:
        try:
            self._call("GET", self._collection_path())
            return True
        except VectorStoreUnavailable:
            return False

    def ensure_collection(self) -> dict:
        """幂等建集合（Cosine，size = EMBEDDING_DIM），再建 class_id / material_id payload 索引。"""
        result = self._call(
            "PUT",
            self._collection_path(),
            {"vectors": {"size": self.config.dim, "distance": "Cosine"}},
        )
        for field_name in ("class_id", "material_id"):
            try:
                self._call(
                    "PUT",
                    self._collection_path(f"/index?wait=true"),
                    {"field_name": field_name, "field_schema": "integer"},
                )
            except VectorStoreUnavailable:
                # 索引已存在或后端不支持时不影响主流程
                pass
        return result

    # ------------------------------------------------------------- 写入

    def upsert(self, points: list[dict]) -> dict:
        """points 元素形如 {"id", "vector", "payload"}。payload MUST NOT 含正文。"""
        if not points:
            return {}
        for p in points:
            payload = p.get("payload") or {}
            if "body_text" in payload:
                raise ValueError("向量库 payload 不得包含正文（design.md D4）")
            if "class_id" not in payload:
                raise ValueError("向量库 payload 必须包含 class_id（design.md §5.2）")
        return self._call("PUT", self._collection_path("/points?wait=true"), {"points": points})

    def delete_by_material(self, material_id: int) -> dict:
        """重建前清理：按 payload filter 删除该材料的全部旧向量。"""
        return self._call(
            "POST",
            self._collection_path("/points/delete?wait=true"),
            {"filter": {"must": [{"key": "material_id", "match": {"value": int(material_id)}}]}},
        )

    def delete_by_ids(self, ids: list[int]) -> dict:
        if not ids:
            return {}
        return self._call(
            "POST", self._collection_path("/points/delete?wait=true"), {"points": [int(i) for i in ids]}
        )

    # ------------------------------------------------------------- 检索

    def search(
        self,
        vector: list[float],
        class_id: int,
        limit: int,
        score_threshold: float | None = None,
    ) -> list[dict]:
        """带 `class_id` **pre-filter** 的向量检索（班级边界在向量侧同样前置）。"""
        body: dict = {
            "vector": vector,
            "limit": int(limit),
            "with_payload": True,
            "with_vector": False,
            "filter": {"must": [{"key": "class_id", "match": {"value": int(class_id)}}]},
        }
        if score_threshold is not None:
            body["score_threshold"] = float(score_threshold)
        resp = self._call("POST", self._collection_path("/points/search"), body)
        return list(resp.get("result") or [])

    def count(self, class_id: int | None = None) -> int:
        body: dict = {"exact": True}
        if class_id is not None:
            body["filter"] = {"must": [{"key": "class_id", "match": {"value": int(class_id)}}]}
        resp = self._call("POST", self._collection_path("/points/count"), body)
        return int((resp.get("result") or {}).get("count") or 0)

    def points_by_material(self, material_id: int, limit: int = 256) -> list[dict]:
        """按 `material_id` 取点（**只要 payload，不要向量**）。

        用于 `GET /api/materials/<id>/vectors` 的「关系库行 ↔ 向量库记录」对照：
        接口**不回传向量分量**，所以这里显式 `with_vector=False`，从数据源上就不取。
        """
        body: dict = {
            "filter": {"must": [{"key": "material_id", "match": {"value": int(material_id)}}]},
            "limit": int(limit),
            "with_payload": True,
            "with_vector": False,
        }
        resp = self._call("POST", self._collection_path("/points"), body)
        result = resp.get("result")
        if isinstance(result, dict):          # 部分版本返回 {"points": [...]}
            return list(result.get("points") or [])
        return list(result or [])


def client_for(app=None, transport=None) -> QdrantClient:
    return QdrantClient(QdrantConfig.from_app(app), transport=transport)
