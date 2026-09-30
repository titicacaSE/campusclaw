"""单元测试公共夹具：把「临时库 + Flask app + 替身注入 + 建索引」收在一处。

单元测试关心的是**函数行为**（阈值、RRF、偏移、转义…），不该关心 HTTP 与容器。
所以这里只做三件事：给一条干净的 SQLite、把外部依赖换成替身、提供一个已建好
索引的本班语料。
"""
from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from app import create_app, db, gateway, indexing, vector_store
from app.chunking import ChunkConfig
from app.seed import seed_database
from app.vector_store import QdrantClient, QdrantConfig
from scripts.qdrant_stub import StubQdrant, StubError
from tests.stubs import DEFAULT_DIM, FakeGateway

SECRET = "unit-harness-secret-0123456789abcd"
EMBED_KEY = "sk-embed-UNIT-SENTINEL"
CHAT_KEY = "sk-chat-UNIT-SENTINEL"
QDRANT_KEY = "qdrant-UNIT-SENTINEL"


class Harness:
    def __init__(self, *, dim: int = DEFAULT_DIM, index: bool = True,
                 strategy: str = "custom", max_chars: int = 150, overlap_percent: int = 0):
        self.dim = int(dim)
        self.tmp = Path(tempfile.mkdtemp(prefix="campusclaw-unit-"))
        self.db_path = self.tmp / "app.db"
        self.upload_dir = self.tmp / "uploads"
        self.qdrant_down = False

        self.stub = StubQdrant(dim=self.dim)
        self.gw = FakeGateway(dim=self.dim)

        self._orig_http_json = gateway.http_json
        self._orig_transport = vector_store._default_transport
        gateway.http_json = self._send_gateway
        vector_store._default_transport = self._send_qdrant

        os.environ.update(
            EMBEDDING_BASE_URL="http://gateway.unit/v1",
            EMBEDDING_API_KEY=EMBED_KEY,
            EMBEDDING_MODEL="unit-embed",
            EMBEDDING_DIM=str(self.dim),
            CHAT_BASE_URL="http://gateway.unit/v1",
            CHAT_API_KEY=CHAT_KEY,
            CHAT_MODEL="unit-chat",
            QDRANT_URL="http://qdrant.unit:6333",
            QDRANT_COLLECTION="campusclaw_chunks",
            QDRANT_API_KEY=QDRANT_KEY,
            SECRET_KEY=SECRET,
        )

        self.app = create_app(
            SECRET_KEY=SECRET, DATABASE=str(self.db_path), UPLOAD_DIR=str(self.upload_dir), TESTING=True
        )
        if index:
            self.index_all(strategy=strategy, max_chars=max_chars, overlap_percent=overlap_percent)

    # -------------------------------------------------- 替身注入
    def _send_gateway(self, url, headers, payload, timeout):
        if url.endswith("/embeddings"):
            return self.gw.embed_transport(url, headers, payload, timeout)
        if url.endswith("/chat/completions"):
            return self.gw.chat_transport(url, headers, payload, timeout)
        raise RuntimeError(f"夹具收到意外 URL：{url}")

    def _send_qdrant(self, method, url, headers, payload, timeout):
        from urllib.parse import urlsplit

        if self.qdrant_down:
            raise vector_store.VectorStoreUnavailable("Qdrant 不可达（夹具注入：down）")
        try:
            return self.stub.handle(method, urlsplit(url).path, payload or {})
        except StubError as exc:
            raise vector_store.VectorStoreUnavailable(
                f"Qdrant 返回 HTTP {exc.status}: {exc.message}"
            ) from exc

    # -------------------------------------------------- 上下文与访问
    @contextmanager
    def ctx(self):
        with self.app.app_context():
            yield self.app

    def qdrant(self) -> QdrantClient:
        return QdrantClient(
            QdrantConfig(url="http://qdrant.unit:6333", collection="campusclaw_chunks",
                         api_key=QDRANT_KEY, dim=self.dim),
            transport=self._send_qdrant,
        )

    def gateway_config(self) -> gateway.GatewayConfig:
        return gateway.GatewayConfig.from_app(self.app)

    def client(self):
        return self.app.test_client()

    def login(self, client, username: str):
        from app.seed import DEMO_PASSWORDS

        return client.post(
            "/login",
            data={"username": username, "password": DEMO_PASSWORDS[username]},
            follow_redirects=False,
        )

    # -------------------------------------------------- 数据
    def index_all(self, *, strategy: str = "custom", max_chars: int = 150,
                  overlap_percent: int = 0) -> None:
        cfg = ChunkConfig(
            strategy=strategy, max_chars=max_chars, overlap_percent=overlap_percent
        ).validate(for_custom=(strategy == "custom"))
        with self.ctx():
            client = self.qdrant()
            client.ensure_collection()
            for row in db.query("SELECT * FROM materials ORDER BY id"):
                body = self.body_of(int(row["id"]))
                indexing.index_material(
                    material_id=int(row["id"]),
                    class_id=int(row["class_id"]),
                    body_text=body,
                    cfg=cfg,
                    qdrant=client,
                    gateway_cfg=self.gateway_config(),
                    embed_transport=self.gw.embed_transport,
                )

    def body_of(self, material_id: int) -> str:
        with self.ctx():
            rows = db.query(
                "SELECT body_text FROM knowledge_entries WHERE material_id = ? ORDER BY ord",
                (int(material_id),),
            )
        return "\n\n".join(r["body_text"] for r in rows if r["body_text"])

    def material_in_class(self, class_id: int) -> int:
        with self.ctx():
            row = db.query_one(
                "SELECT id FROM materials WHERE class_id = ? ORDER BY id LIMIT 1", (int(class_id),)
            )
        return int(row["id"])

    def chunk_of(self, material_id: int) -> int:
        with self.ctx():
            row = db.query_one(
                "SELECT id FROM knowledge_chunks WHERE material_id = ? ORDER BY chunk_index LIMIT 1",
                (int(material_id),),
            )
        return int(row["id"]) if row else None

    # -------------------------------------------------- 便捷访问（自动带上应用上下文）
    def retrieve(self, class_id: int, q: str, **kw):
        from app import retrieval

        with self.ctx():
            return retrieval.retrieve(class_id, q, **kw)

    def search_chunks(self, class_id: int, q: str, **kw):
        with self.ctx():
            return db.search_chunks(class_id, q, **kw)

    def index_warnings(self, class_id: int):
        with self.ctx():
            return [dict(r) for r in db.index_warnings(class_id)]

    def chunks_of(self, material_id: int, class_id: int = 1) -> list[dict]:
        with self.ctx():
            return [dict(r) for r in db.list_chunks(class_id, material_id)]

    def chunk_row(self, chunk_id: int, class_id: int) -> dict | None:
        with self.ctx():
            row = db.get_chunk(chunk_id, class_id)
        return dict(row) if row else None

    def material_ids(self, class_id: int) -> set[int]:
        with self.ctx():
            return {int(r["id"]) for r in db.query(
                "SELECT id FROM materials WHERE class_id=?", (int(class_id),))}

    def chunk_ids(self, class_id: int) -> set[int]:
        with self.ctx():
            return {int(r["id"]) for r in db.query(
                "SELECT id FROM knowledge_chunks WHERE class_id=?", (int(class_id),))}

    def set_material_status(self, material_id: int, status: str) -> None:
        with self.ctx():
            db.execute("UPDATE materials SET index_status=? WHERE id=?", (status, int(material_id)))
            db.get_conn().commit()

    def teardown(self) -> None:
        gateway.http_json = self._orig_http_json
        vector_store._default_transport = self._orig_transport
        shutil.rmtree(self.tmp, ignore_errors=True)


# 种子语料里的真实片段（与 http 验收保持同一批字符串，便于交叉核对）
PHRASE = "倒叙的作用是设置悬念，插叙的作用是补充背景"
B_ONLY = "一元二次方程的四种解法"
UNRELATED = "量子纠缠与薛定谔方程的推导过程是怎样的"
