#!/usr/bin/env python3
"""Qdrant 最小 REST 子集的**测试替身**（本机无 Docker 时的替代验证路径）

它只实现本项目真正用到的那些路由，用于：
  1. `tests/http_retrieval_acceptance.py` 的进程内注入（默认路径，无需任何服务）
  2. 本地手工联调：`python scripts/qdrant_stub.py --port 6333`
     （此时把 `QDRANT_URL=http://127.0.0.1:6333` 指过来即可，不必装 Qdrant）

支持的路由
----------
  PUT    /collections/{name}                     建集合（Cosine，size = EMBEDDING_DIM）
  GET    /collections/{name}                     集合信息（不存在 404）
  PUT    /collections/{name}/index               建 payload 索引（幂等，无实际作用）
  PUT    /collections/{name}/points              写入/覆盖点
  POST   /collections/{name}/points/delete       按 filter 或按 id 列表删除
  POST   /collections/{name}/points/search       余弦检索（支持 class_id filter 与阈值）
  POST   /collections/{name}/points/count        计数（支持 filter）
  POST   /collections/{name}/points              取点（只要 payload，不要向量）

**它不做权限、不做持久化、不做量化**，只用于验证「班级过滤是否前置」「阈值是否生效」
「payload 是否含正文」这类**行为**问题。真实 Qdrant 的行为差异必须另行复核，
`ACCEPTANCE.md` 中已按「替身验证 / 真实 Qdrant 待复核」分别标注。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class StubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _match_value(payload: dict, condition: dict) -> bool:
    """`{"key": k, "match": {"value": v}}` 形式的单条件匹配。"""
    key = condition.get("key")
    expected = (condition.get("match") or {}).get("value")
    actual = (payload or {}).get(key)
    if isinstance(expected, int) and isinstance(actual, (int, float)):
        return int(actual) == int(expected)
    return actual == expected


def _passes(payload: dict, flt: dict | None) -> bool:
    if not flt:
        return True
    for condition in flt.get("must") or []:
        if not _match_value(payload, condition):
            return False
    for condition in flt.get("must_not") or []:
        if _match_value(payload, condition):
            return False
    should = flt.get("should") or []
    if should and not any(_match_value(payload, c) for c in should):
        return False
    return True


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise StubError(400, "向量维度不一致")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class StubQdrant:
    """内存向量库。所有写入即时可见，支持并发读。"""

    def __init__(self, dim: int = 0):
        self.dim = int(dim or 0)
        self.collections: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []          # 调用日志：用于断言"是否真的查了向量库"
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 内部
    def _collection(self, name: str) -> dict:
        col = self.collections.get(name)
        if col is None:
            raise StubError(404, f"collection {name} not found")
        return col

    # ------------------------------------------------------------ 路由
    def handle(self, method: str, path: str, payload: dict | None = None) -> dict:
        payload = payload or {}
        parts = [unquote(p) for p in path.strip("/").split("/") if p]
        with self._lock:
            self.calls.append((method.upper(), path))

        if len(parts) >= 2 and parts[0] == "collections":
            name = parts[1]
            tail = parts[2:]
            if not tail:
                if method.upper() == "PUT":
                    return self._create(name, payload)
                if method.upper() == "GET":
                    col = self._collection(name)
                    return {"result": {"status": "green", "config": {"params": {
                        "vectors": {"size": col["dim"], "distance": "Cosine"}}}},
                        "status": "ok"}
                raise StubError(405, f"unsupported method {method} on /collections/{{name}}")
            if tail == ["index"]:
                self._collection(name)
                return {"result": {"operation_id": 0, "status": "completed"}, "status": "ok"}
            if tail == ["points"]:
                if method.upper() == "PUT":
                    count = self.upsert(name, payload.get("points") or [])
                    return {"result": {"operation_id": 0, "status": "completed",
                                       "inserted": count}, "status": "ok"}
                if method.upper() == "POST":
                    return self._scroll(name, payload)
            if tail == ["points", "search"]:
                return {"result": self._search(name, payload), "status": "ok"}
            if tail == ["points", "count"]:
                return {"result": {"count": self._count(name, payload)}, "status": "ok"}
            if tail == ["points", "delete"]:
                self._delete(name, payload)
                return {"result": {"operation_id": 0, "status": "completed"}, "status": "ok"}

        raise StubError(404, f"no route for {method} {path}")

    # ------------------------------------------------------------ 实现
    def _create(self, name: str, payload: dict) -> dict:
        vectors = payload.get("vectors") or {}
        size = int(vectors.get("size") or self.dim or 0)
        distance = str(vectors.get("distance") or "Cosine")
        if distance != "Cosine":
            raise StubError(400, "本替身只实现 Cosine 距离")
        if size <= 0:
            raise StubError(400, "vectors.size 必须为正整数")
        if self.dim and size != self.dim:
            raise StubError(400, f"集合维度 {self.dim} 与请求 {size} 不一致")
        if name not in self.collections:                 # PUT 是幂等建集合
            self.collections[name] = {"dim": size, "distance": distance, "points": {}}
        return {"result": True, "status": "ok"}

    # 写入点在 handle() 中以 `tail == ["points"] and PUT` 分派，这里做真正的 upsert
    def upsert(self, name: str, points: list[dict]) -> int:
        col = self._collection(name)
        for point in points:
            vector = point.get("vector")
            if not isinstance(vector, list) or len(vector) != col["dim"]:
                raise StubError(400, "向量维度与集合不一致")
            payload = point.get("payload") or {}
            if "body_text" in payload:
                raise StubError(400, "payload 不得包含正文")
            col["points"][int(point["id"])] = {
                "id": int(point["id"]),
                "vector": [float(x) for x in vector],
                "payload": dict(payload),
            }
        return len(points)

    def _delete(self, name: str, payload: dict) -> None:
        col = self._collection(name)
        if "points" in payload:
            for pid in payload.get("points") or []:
                col["points"].pop(int(pid), None)
            return
        flt = payload.get("filter")
        for pid in [pid for pid, p in col["points"].items() if _passes(p["payload"], flt)]:
            col["points"].pop(pid, None)

    def _search(self, name: str, payload: dict) -> list[dict]:
        """余弦检索。**filter 在候选生成之前生效**（与真实 Qdrant 的 pre-filter 语义一致）。"""
        col = self._collection(name)
        vector = payload.get("vector") or []
        threshold = payload.get("score_threshold")
        flt = payload.get("filter")
        scored = []
        for point in col["points"].values():
            if not _passes(point["payload"], flt):
                continue
            score = _cosine([float(x) for x in vector], point["vector"])
            if threshold is not None and score < float(threshold):
                continue
            scored.append({"id": point["id"], "version": 0, "score": score,
                           "payload": dict(point["payload"])})
        scored.sort(key=lambda p: -p["score"])
        return scored[: int(payload.get("limit") or 10)]

    def _count(self, name: str, payload: dict) -> int:
        col = self._collection(name)
        flt = payload.get("filter")
        return sum(1 for p in col["points"].values() if _passes(p["payload"], flt))

    def _scroll(self, name: str, payload: dict) -> dict:
        col = self._collection(name)
        flt = payload.get("filter")
        rows = [p for p in col["points"].values() if _passes(p["payload"], flt)]
        rows.sort(key=lambda p: p["id"])
        limit = int(payload.get("limit") or 10)
        out = [{"id": p["id"], "payload": dict(p["payload"])} for p in rows[:limit]]
        return {"result": {"points": out, "next_page_offset": None}, "status": "ok"}

    def reset(self) -> None:
        with self._lock:
            self.collections.clear()
            self.calls.clear()


# ---------------------------------------------------------------- 进程内注入用传输层


def make_transport(store: StubQdrant, *, down: bool = False):
    """返回一个与 `vector_store._default_transport` 同签名的传输函数。

    `down=True` 时抛 `VectorStoreUnavailable`，用于模拟「Qdrant 停掉」。
    """
    from app.vector_store import VectorStoreUnavailable

    def transport(method: str, url: str, headers: dict, payload: dict | None, timeout: int) -> dict:
        if down:
            raise VectorStoreUnavailable("Qdrant 不可达（替身注入：down）")
        path = urlsplit(url).path
        try:
            return store.handle(method, path, payload or {})
        except StubError as exc:
            raise VectorStoreUnavailable(f"Qdrant 返回 HTTP {exc.status}: {exc.message}") from exc

    return transport


# ---------------------------------------------------------------- 独立 HTTP 服务


class _Handler(BaseHTTPRequestHandler):
    store: StubQdrant = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):  # 静音
        pass

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _dispatch(self, method: str) -> None:
        payload = self._read_body()
        try:
            self._send(200, self.store.handle(method, urlsplit(self.path).path, payload))
        except StubError as exc:
            self._send(exc.status, {"status": {"error": exc.message}})

    def do_GET(self):
        self._dispatch("GET")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_POST(self):
        self._dispatch("POST")

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def serve(port: int = 6333, dim: int = 0) -> ThreadingHTTPServer:
    _Handler.store = StubQdrant(dim=dim)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    return httpd


def main() -> int:
    parser = argparse.ArgumentParser(description="CampusClaw 用的 Qdrant 测试替身")
    parser.add_argument("--port", type=int, default=6333)
    parser.add_argument("--dim", type=int, default=0, help="集合维度；0 表示以建集合请求为准")
    args = parser.parse_args()

    httpd = serve(port=args.port, dim=args.dim)
    print(f"[qdrant_stub] 监听 http://127.0.0.1:{args.port}（内存态，仅用于本地验证）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
