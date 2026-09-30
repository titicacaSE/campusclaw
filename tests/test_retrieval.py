"""检索链路单元测试（T1 / T2 / T4 / T5 / T7）

覆盖 tasks.md 的类名：GatewayConfigTests / GatewayRequestShapeTests / EmbeddingDimGuardTests /
IndexWarningTests / QdrantClientTests / VectorPayloadTests / VectorPrefilterTests /
ThresholdTests / RrfFormulaTests / InputValidationTests / StableOrderTests / MatchingTests /
HitFieldsTests / CoordinateSystemTests。

为什么这些判据必须在「函数层」再测一遍
--------------------------------------
HTTP 验收证明了接口**接得上、码对**；但阈值 0.35 的**等号该保留还是丢弃**、
RRF 是「两路相加」还是「各自 1/(60+rank) 再相加」、缺席的一路要不要贡献分数，
这些必须在最小单元上钉死，否则换一条调用路径就可能悄悄漂移。
"""
from __future__ import annotations

import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app, db, gateway, indexing, knowledge, retrieval  # noqa: E402
from app.chunking import ChunkConfig, preprocess  # noqa: E402
from app.retrieval import (  # noqa: E402
    MAX_LIMIT,
    MAX_OFFSET,
    NO_EVIDENCE_MESSAGE,
    RRF_K,
    VECTOR_MIN_SCORE,
    Hit,
    QueryError,
    build_chunk_records,
    retrieve,
    validate_query,
    vector_candidates,
)
from app.vector_store import (  # noqa: E402
    QdrantClient,
    QdrantConfig,
    VectorStoreUnavailable,
)
from scripts.qdrant_stub import StubError, StubQdrant  # noqa: E402
from tests.harness import B_ONLY, PHRASE, UNRELATED, Harness  # noqa: E402

UPSTREAM_KEYS = ("EMBEDDING_BASE_URL", "EMBEDDING_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_DIM",
                 "CHAT_BASE_URL", "CHAT_API_KEY", "CHAT_MODEL", "QDRANT_URL", "QDRANT_COLLECTION",
                 "QDRANT_API_KEY")


# ===================================================================== 小工具


def insert_material(h: Harness, body: str, *, class_id: int = 1, title: str = "单元测试材料",
                    max_chars: int = 150) -> tuple[int, list[int]]:
    """直接落一份材料 + 切片（不走 HTTP），供需要精确控制内容的用例使用。"""
    with h.ctx():
        mid, _entry = knowledge.persist_material(
            title=title, class_id=class_id, file_path=f"/tmp/{title}.md",
            original_filename=f"{title}.md", ext=".md", size_bytes=len(body.encode()),
            uploaded_by=1, body_text=body,
        )
        cfg = ChunkConfig(strategy="custom", max_chars=max_chars, overlap_percent=0)
        records, _sha = build_chunk_records(mid, class_id, body, cfg)
        db.replace_chunks(mid, class_id, records)
        # 夹具自己拆开提交，所以必须显式提交：否则下一个 ctx 用的是新连接，看不到这批切片，
        # 关键字检索会「明明插了却查不到」。
        db.get_conn().commit()
        rows = db.list_chunks(class_id, mid)
    return mid, [int(r["id"]) for r in rows]


def forced_embed(vector: list[float]):
    """返回一个「无论输入什么都给出同一个向量」的嵌入传输层，用来精确控制余弦。"""
    calls = {"n": 0}

    def transport(url, headers, payload, timeout):
        calls["n"] += 1
        return {"data": [{"embedding": [float(x) for x in vector]} for _ in payload["input"]]}

    transport.calls = calls
    return transport


def put_point(h: Harness, chunk_id: int, material_id: int, class_id: int, vector: list[float],
              chunk_index: int = 0, chunk_sha256: str = "x") -> None:
    client = h.qdrant()
    client.ensure_collection()          # 幂等：用例可能用 index=False 的夹具，集合尚未建
    client.upsert([{
        "id": int(chunk_id),
        "vector": [float(v) for v in vector],
        "payload": {"chunk_id": int(chunk_id), "material_id": int(material_id),
                    "class_id": int(class_id), "chunk_index": int(chunk_index),
                    "chunk_sha256": chunk_sha256},
    }])


class _Env:
    """临时把上游配置从环境变量里摘掉，用来验证「缺配置也能启动、按降级语义响应」。"""

    def __enter__(self):
        self.saved = {k: os.environ.pop(k, None) for k in UPSTREAM_KEYS}
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def bare_app(tmp: Path):
    """没有任何上游配置的裸应用（只有 SECRET_KEY 与本地库）。"""
    return create_app(
        SECRET_KEY="bare-app-secret-0123456789abcdef",
        DATABASE=str(tmp / "app.db"),
        UPLOAD_DIR=str(tmp / "uploads"),
        TESTING=True,
    )


# ===================================================================== T1 网关适配
class GatewayConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="campusclaw-gw-"))
        self._env = _Env().__enter__()

    def tearDown(self):
        self._env.__exit__()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_credentials_degrade_but_do_not_block_startup(self):
        app = bare_app(self.tmp)
        cfg = gateway.GatewayConfig.from_app(app)
        self.assertFalse(cfg.embedding_ready)
        self.assertFalse(cfg.chat_ready)
        self.assertTrue(app.url_map)                       # 应用照常可用

    def test_missing_credentials_raise_typed_errors(self):
        cfg = gateway.GatewayConfig.from_app(bare_app(self.tmp))
        with self.assertRaises(gateway.EmbeddingUnavailable):
            gateway.embed(["x"], cfg=cfg)
        with self.assertRaises(gateway.ChatUnavailable):
            gateway.chat([{"role": "user", "content": "hi"}], cfg=cfg)

    def test_keyword_works_and_vector_returns_503_without_upstream(self):
        app = bare_app(self.tmp)
        client = app.test_client()
        from app.seed import DEMO_PASSWORDS

        client.post("/login", data={"username": "teacher_a",
                                    "password": DEMO_PASSWORDS["teacher_a"]})
        r = client.post("/api/search", json={"q": "语文", "mode": "keyword"})
        self.assertEqual(r.status_code, 200)
        for mode in ("vector", "hybrid"):
            r = client.post("/api/search", json={"q": "语文", "mode": mode})
            self.assertEqual(r.status_code, 503, mode)
            self.assertIn(r.get_json()["error"],
                          ("vector_store_unavailable", "embedding_unavailable"))

    def test_redacted_view_never_contains_keys(self):
        h = Harness(index=False)
        self.addCleanup(h.teardown)
        redacted = h.gateway_config().redacted()
        self.assertNotIn("api_key", str(redacted))
        self.assertIn("embedding_ready", redacted)

    def test_env_is_used_when_app_config_is_empty(self):
        os.environ["EMBEDDING_MODEL"] = "from-env"
        self.assertEqual(gateway.GatewayConfig.from_app().embedding_model, "from-env")


class GatewayRequestShapeTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(index=False)
        self.addCleanup(self.h.teardown)

    def test_embedding_request_contains_only_model_and_input(self):
        gateway.embed(["甲", "乙"], cfg=self.h.gateway_config(),
                      transport=self.h.gw.embed_transport)
        call = self.h.gw.embed_calls[-1]
        self.assertEqual(set(call["payload"]), {"model", "input"})
        self.assertEqual(call["payload"]["model"], "unit-embed")
        self.assertTrue(call["url"].endswith("/embeddings"), call["url"])
        self.assertIn("Authorization", call["headers"])

    def test_embedding_is_batched_in_chunks_of_64(self):
        gateway.embed([f"文本{i}" for i in range(130)], cfg=self.h.gateway_config(),
                      transport=self.h.gw.embed_transport)
        self.assertEqual([len(c["payload"]["input"]) for c in self.h.gw.embed_calls], [64, 64, 2])

    def test_empty_input_makes_no_call(self):
        self.assertEqual(
            gateway.embed([], cfg=self.h.gateway_config(), transport=self.h.gw.embed_transport), []
        )
        self.assertEqual(self.h.gw.embed_calls, [])

    def test_chat_request_carries_messages_and_model(self):
        gateway.chat([{"role": "user", "content": "你好"}], cfg=self.h.gateway_config(),
                     transport=self.h.gw.chat_transport)
        call = self.h.gw.chat_calls[-1]
        self.assertEqual(call["payload"]["model"], "unit-chat")
        self.assertEqual(call["payload"]["messages"][0]["content"], "你好")
        self.assertTrue(call["url"].endswith("/chat/completions"), call["url"])


class EmbeddingDimGuardTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(index=False)
        self.addCleanup(self.h.teardown)

    def test_dimension_mismatch_raises(self):
        self.h.gw.dim_mismatch = True
        with self.assertRaises(gateway.EmbeddingDimMismatch):
            gateway.embed(["甲"], cfg=self.h.gateway_config(),
                          transport=self.h.gw.embed_transport)

    def test_dimension_mismatch_writes_no_vector_and_marks_failed(self):
        body = "光合作用发生在叶绿体并释放氧气，原料是二氧化碳和水，同时需要光照。" * 2
        self.h.gw.dim_mismatch = True
        with self.h.ctx():
            mid, _e = knowledge.persist_material(
                title="维度不符", class_id=1, file_path="/tmp/x.md", original_filename="x.md",
                ext=".md", size_bytes=len(body), uploaded_by=1, body_text=body,
            )
            outcome = indexing.index_material(
                material_id=mid, class_id=1, body_text=body,
                cfg=ChunkConfig(strategy="custom", max_chars=150, overlap_percent=0),
                qdrant=self.h.qdrant(), gateway_cfg=self.h.gateway_config(),
                embed_transport=self.h.gw.embed_transport,
            )
            self.assertEqual(outcome.index_status, indexing.STATUS_FAILED)
            self.assertEqual(outcome.vectors_written, 0)
            self.assertIn("维度", outcome.vector_error or "")
            self.assertEqual({r["index_status"] for r in db.list_chunks(1, mid)},
                             {indexing.STATUS_FAILED})
            self.assertEqual(self.h.stub.collections["campusclaw_chunks"]["points"], {})

    def test_count_mismatch_is_treated_as_failure(self):
        cfg = self.h.gateway_config()

        def bad_transport(url, headers, payload, timeout):
            return {"data": [{"embedding": [0.1] * cfg.embedding_dim}]}     # 少于输入条数

        with self.assertRaises(gateway.EmbeddingUnavailable):
            gateway.embed(["甲", "乙"], cfg=cfg, transport=bad_transport)


# ===================================================================== T2 数据层
class IndexWarningTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(index=False)
        self.addCleanup(self.h.teardown)

    def test_pending_materials_are_reported(self):
        warnings = self.h.index_warnings(1)
        self.assertTrue(warnings)
        self.assertTrue(all(w["index_status"] != "indexed" for w in warnings))

    def test_indexed_materials_are_excluded(self):
        mid = min(self.h.material_ids(1))
        self.h.set_material_status(mid, "indexed")
        self.assertNotIn(mid, [w["material_id"] for w in self.h.index_warnings(1)])

    def test_warnings_are_class_scoped(self):
        got = {w["material_id"] for w in self.h.index_warnings(1)}
        self.assertEqual(got, self.h.material_ids(1))
        self.assertFalse(got & self.h.material_ids(2))

    def test_degraded_flag_follows_warnings(self):
        result = self.h.retrieve(1, PHRASE, mode="keyword")
        self.assertTrue(result.degraded)
        self.assertTrue(result.index_warnings)

    def test_no_warnings_means_not_degraded(self):
        h = Harness(index=True)
        self.addCleanup(h.teardown)
        self.assertEqual(h.index_warnings(1), [])
        self.assertFalse(h.retrieve(1, PHRASE, mode="keyword").degraded)


# ===================================================================== T4 向量客户端
class QdrantClientTests(unittest.TestCase):
    def setUp(self):
        self.store = StubQdrant(dim=4)
        self.client = QdrantClient(
            QdrantConfig(url="http://stub", collection="c", dim=4), transport=self._transport
        )
        self.down = False

    def _transport(self, method, url, headers, payload, timeout):
        from urllib.parse import urlsplit

        if self.down:
            raise VectorStoreUnavailable("down")
        try:
            return self.store.handle(method, urlsplit(url).path, payload or {})
        except StubError as exc:
            raise VectorStoreUnavailable(str(exc)) from exc

    def test_ensure_collection_is_idempotent_cosine(self):
        self.client.ensure_collection()
        self.client.ensure_collection()
        self.assertEqual(self.store.collections["c"]["distance"], "Cosine")
        self.assertEqual(self.store.collections["c"]["dim"], 4)
        self.assertTrue(self.client.available())

    def test_upsert_search_count_scroll_delete(self):
        self.client.ensure_collection()
        self.client.upsert([
            {"id": 1, "vector": [1, 0, 0, 0],
             "payload": {"chunk_id": 1, "material_id": 10, "class_id": 1}},
            {"id": 2, "vector": [0, 1, 0, 0],
             "payload": {"chunk_id": 2, "material_id": 10, "class_id": 2}},
        ])
        self.assertEqual(self.client.count(), 2)
        self.assertEqual(self.client.count(class_id=1), 1)
        hits = self.client.search([1, 0, 0, 0], class_id=1, limit=10)
        self.assertEqual([h["payload"]["chunk_id"] for h in hits], [1])
        self.assertEqual(self.client.points_by_material(10)[0]["id"], 1)
        self.client.delete_by_material(10)
        self.assertEqual(self.client.count(), 0)

    def test_delete_by_ids(self):
        self.client.ensure_collection()
        self.client.upsert([{"id": i, "vector": [1, 0, 0, 0],
                             "payload": {"chunk_id": i, "material_id": 1, "class_id": 1}}
                            for i in (1, 2, 3)])
        self.client.delete_by_ids([2])
        self.assertEqual(self.client.count(), 2)

    def test_unconfigured_client_raises_instead_of_silently_succeeding(self):
        client = QdrantClient(QdrantConfig(url="", dim=0), transport=self._transport)
        with self.assertRaises(VectorStoreUnavailable):
            client.ensure_collection()
        self.assertFalse(client.available())

    def test_unreachable_backend_raises(self):
        self.down = True
        with self.assertRaises(VectorStoreUnavailable):
            self.client.count()
        self.assertFalse(self.client.available())

    def test_wrong_dimension_is_rejected(self):
        self.client.ensure_collection()
        with self.assertRaises(VectorStoreUnavailable):
            self.client.upsert([{"id": 1, "vector": [1, 0], "payload": {"class_id": 1}}])

    def test_search_threshold_is_forwarded(self):
        self.client.ensure_collection()
        self.client.upsert([
            {"id": 1, "vector": [1.0, 0, 0, 0], "payload": {"chunk_id": 1, "class_id": 1}},
            {"id": 2, "vector": [0.2, math.sqrt(0.96), 0, 0], "payload": {"chunk_id": 2, "class_id": 1}},
        ])
        self.assertEqual(len(self.client.search([1, 0, 0, 0], class_id=1, limit=10)), 2)
        filtered = self.client.search([1, 0, 0, 0], class_id=1, limit=10, score_threshold=0.35)
        self.assertEqual([h["payload"]["chunk_id"] for h in filtered], [1])


class VectorPayloadTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(index=True)
        self.addCleanup(self.h.teardown)

    def test_point_id_is_chunk_id_and_payload_has_no_body(self):
        points = self.h.stub.collections["campusclaw_chunks"]["points"]
        self.assertTrue(points)
        self.assertTrue(set(points) <= self.h.chunk_ids(1) | self.h.chunk_ids(2))
        for point in points.values():
            payload = point["payload"]
            self.assertEqual(payload["chunk_id"], point["id"])
            for key in ("class_id", "material_id", "chunk_index", "chunk_sha256"):
                self.assertIn(key, payload)
            self.assertNotIn("body_text", payload)
            self.assertNotIn("text", payload)

    def test_upsert_refuses_payload_with_body_text(self):
        with self.assertRaises(ValueError):
            self.h.qdrant().upsert([{"id": 1, "vector": [0.0] * self.h.dim,
                                     "payload": {"class_id": 1, "body_text": "正文"}}])

    def test_upsert_refuses_payload_without_class_id(self):
        with self.assertRaises(ValueError):
            self.h.qdrant().upsert([{"id": 1, "vector": [0.0] * self.h.dim, "payload": {}}])

    def test_vector_count_matches_chunk_count_per_class(self):
        with self.h.ctx():
            for class_id in (1, 2):
                n = db.query_one(
                    "SELECT COUNT(*) AS n FROM knowledge_chunks WHERE class_id=?", (class_id,)
                )["n"]
                self.assertEqual(self.h.qdrant().count(class_id=class_id), n)

    def test_chunk_sha256_in_payload_matches_db(self):
        points = self.h.stub.collections["campusclaw_chunks"]["points"]
        sample = next(iter(points.values()))
        row = self.h.chunk_row(sample["id"], sample["payload"]["class_id"])
        self.assertEqual(row["chunk_sha256"], sample["payload"]["chunk_sha256"])


class VectorPrefilterTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(index=True)
        self.addCleanup(self.h.teardown)

    def _vector_hits(self, class_id, q):
        return self.h.retrieve(class_id, q, mode="vector", limit=10,
                               qdrant=self.h.qdrant(), cfg=self.h.gateway_config(),
                               embed_transport=self.h.gw.embed_transport)

    def test_vector_search_never_returns_other_class_chunks(self):
        b_materials = self.h.material_ids(2)
        self.assertEqual(self._vector_hits(1, B_ONLY).hits, [])

        own = self._vector_hits(2, B_ONLY)
        self.assertTrue(own.hits)
        self.assertTrue({h.material_id for h in own.hits} <= b_materials)

    def test_hybrid_also_excludes_other_class(self):
        result = self.h.retrieve(1, B_ONLY, mode="hybrid", limit=10, qdrant=self.h.qdrant(),
                                 cfg=self.h.gateway_config(),
                                 embed_transport=self.h.gw.embed_transport)
        self.assertEqual(result.hits, [])

    def test_search_actually_hits_the_vector_backend(self):
        self.h.stub.calls.clear()
        self._vector_hits(1, PHRASE)
        self.assertTrue([c for c in self.h.stub.calls if c[1].endswith("/points/search")])

    def test_backend_filter_is_enforced_by_the_store_itself(self):
        points = self.h.qdrant().search([0.1] * self.h.dim, class_id=2, limit=100)
        self.assertTrue(points)
        self.assertTrue(all(p["payload"]["class_id"] == 2 for p in points))

    def test_candidates_recheck_class_id_on_the_relation_side(self):
        """回表时的 class_id 是第二道闸：关系库查不到就丢弃，不因向量命中就采信。"""
        b_chunk = min(self.h.chunk_ids(2))
        put_point(self.h, b_chunk, material_id=999999, class_id=1,
                  vector=[1.0] + [0.0] * (self.h.dim - 1))
        with self.h.ctx():
            candidates, _vec = vector_candidates(
                1, "任意查询", qdrant=self.h.qdrant(), cfg=self.h.gateway_config(),
                embed_transport=forced_embed([1.0] + [0.0] * (self.h.dim - 1)),
            )
        self.assertNotIn(b_chunk, {int(row["id"]) for _s, row in candidates})


class ThresholdTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(dim=4, index=False)
        self.addCleanup(self.h.teardown)
        self.mid, self.chunks = insert_material(
            self.h, "阈值材料\n\n阈值测试用正文，确保有切片可命中，并保证长度足够。"
        )
        self.assertEqual(len(self.chunks), 1)

    def _candidates(self, point_vector):
        put_point(self.h, self.chunks[0], self.mid, 1, point_vector)
        with self.h.ctx():                    # 回表要读关系库，必须在应用上下文里
            out, _vec = vector_candidates(
                1, "查询", qdrant=self.h.qdrant(), cfg=self.h.gateway_config(),
                embed_transport=forced_embed([1.0, 0.0, 0.0, 0.0]),
            )
        return [int(row["id"]) for _s, row in out]

    def test_above_threshold_is_kept(self):
        self.assertEqual(self._candidates([0.9, math.sqrt(1 - 0.81), 0.0, 0.0]), [self.chunks[0]])

    def test_below_threshold_is_dropped(self):
        self.assertEqual(self._candidates([0.34, math.sqrt(1 - 0.34 ** 2), 0.0, 0.0]), [])

    def test_exactly_at_threshold_is_kept(self):
        """规约写的是「余弦 < 0.35 丢弃」—— 等号保留。"""
        vector = [VECTOR_MIN_SCORE, math.sqrt(1 - VECTOR_MIN_SCORE ** 2), 0.0, 0.0]
        self.assertEqual(self._candidates(vector), [self.chunks[0]])

    def test_threshold_applies_to_the_vector_path_of_hybrid(self):
        put_point(self.h, self.chunks[0], self.mid, 1, [0.1, math.sqrt(0.99), 0.0, 0.0])
        result = self.h.retrieve(1, "完全不存在的词", mode="hybrid", limit=10,
                                 qdrant=self.h.qdrant(), cfg=self.h.gateway_config(),
                                 embed_transport=forced_embed([1.0, 0.0, 0.0, 0.0]))
        self.assertEqual(result.hits, [])

    def test_threshold_constant_matches_the_slides(self):
        self.assertEqual(VECTOR_MIN_SCORE, 0.35)


# ===================================================================== T5 融合与输入
class RrfFormulaTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(dim=4, index=False)
        self.addCleanup(self.h.teardown)

    def test_unit_formula(self):
        self.assertEqual(RRF_K, 60)
        self.assertEqual(retrieval._rrf(1), 1 / 61)
        self.assertEqual(retrieval._rrf(2), 1 / 62)
        self.assertEqual(retrieval._rrf(60), 1 / 120)

    def _two_chunks(self):
        """两段各 80 字、中间空行：段落边界优先，稳定切成 2 片（每片 ≥32 字不会被合并）。"""
        self.h.qdrant().ensure_collection()      # 真部署里集合先于检索存在
        return insert_material(self.h, "甲乙丙丁" * 20 + "\n\n" + "戊己庚辛" * 20)

    def test_both_paths_contribute_and_are_summed(self):
        mid, chunks = self._two_chunks()
        put_point(self.h, chunks[0], mid, 1, [0.0, 1.0, 0.0, 0.0])
        result = self.h.retrieve(1, "甲乙丙丁", mode="hybrid", limit=10, qdrant=self.h.qdrant(),
                                 cfg=self.h.gateway_config(),
                                 embed_transport=forced_embed([0.0, 1.0, 0.0, 0.0]))
        hit = next(h for h in result.hits if h.chunk_id == chunks[0])
        self.assertIsNotNone(hit.rank_keyword)
        self.assertIsNotNone(hit.rank_vector)
        self.assertAlmostEqual(
            hit.score_final,
            retrieval._rrf(hit.rank_keyword) + retrieval._rrf(hit.rank_vector),
            places=12,
        )

    def test_absent_path_contributes_no_score(self):
        mid, chunks = self._two_chunks()
        put_point(self.h, chunks[1], mid, 1, [0.0, 1.0, 0.0, 0.0])
        result = self.h.retrieve(1, "完全不存在的检索词", mode="hybrid", limit=10,
                                 qdrant=self.h.qdrant(), cfg=self.h.gateway_config(),
                                 embed_transport=forced_embed([0.0, 1.0, 0.0, 0.0]))
        self.assertTrue(result.hits)
        for hit in result.hits:
            self.assertIsNone(hit.rank_keyword)
            self.assertIsNone(hit.score_keyword)
            self.assertAlmostEqual(hit.score_final, retrieval._rrf(hit.rank_vector), places=12)

    def test_keyword_only_candidate_keeps_keyword_score_only(self):
        self._two_chunks()
        result = self.h.retrieve(1, "甲乙丙丁", mode="hybrid", limit=10, qdrant=self.h.qdrant(),
                                 cfg=self.h.gateway_config(),
                                 embed_transport=forced_embed([1.0, 0.0, 0.0, 0.0]))
        self.assertTrue(result.hits)
        for hit in result.hits:
            if hit.rank_vector is None:
                self.assertAlmostEqual(hit.score_final, retrieval._rrf(hit.rank_keyword), places=12)

    def test_scores_are_sorted_descending(self):
        self._two_chunks()
        result = self.h.retrieve(1, "甲乙丙丁", mode="hybrid", limit=10, qdrant=self.h.qdrant(),
                                 cfg=self.h.gateway_config(),
                                 embed_transport=forced_embed([1.0, 0.0, 0.0, 0.0]))
        scores = [h.score_final for h in result.hits]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_keyword_mode_never_touches_the_vector_store(self):
        self._two_chunks()
        self.h.stub.calls.clear()
        self.h.gw.embed_calls.clear()
        result = self.h.retrieve(1, "甲乙丙丁", mode="keyword", limit=10)
        self.assertTrue(result.hits)
        self.assertEqual(self.h.stub.calls, [])
        self.assertEqual(self.h.gw.embed_calls, [])
        self.assertIsNone(result.query_vector)


class InputValidationTests(unittest.TestCase):
    def test_query_is_trimmed(self):
        q, limit, offset, mode = validate_query("  甲乙  ")
        self.assertEqual((q, limit, offset, mode), ("甲乙", 10, 0, "hybrid"))

    def test_empty_or_blank_query_rejected(self):
        for bad in ("", "   ", "\n\t", None):
            with self.assertRaises(QueryError):
                validate_query(bad)

    def test_control_characters_rejected(self):
        for bad in ("甲\x00乙", "甲\x07乙", "甲\x1f乙"):
            with self.assertRaises(QueryError):
                validate_query(bad)

    def test_overlong_query_rejected_not_truncated(self):
        with self.assertRaises(QueryError):
            validate_query("回" * 301)
        self.assertEqual(len(validate_query("回" * 300)[0]), 300)

    def test_limit_and_offset_bounds(self):
        self.assertEqual(validate_query("甲", 1, 0)[1:3], (1, 0))
        self.assertEqual(validate_query("甲", MAX_LIMIT, MAX_OFFSET)[1:3], (MAX_LIMIT, MAX_OFFSET))
        for bad in (0, -1, MAX_LIMIT + 1, 999):
            with self.assertRaises(QueryError):
                validate_query("甲", bad)
        for bad in (-1, MAX_OFFSET + 1):
            with self.assertRaises(QueryError):
                validate_query("甲", 10, bad)

    def test_non_integer_and_boolean_params_rejected(self):
        for bad in ("abc", 1.5, True):
            with self.assertRaises(QueryError):
                validate_query("甲", bad)
        for bad in ("abc", False):
            with self.assertRaises(QueryError):
                validate_query("甲", 10, bad)

    def test_unknown_mode_rejected(self):
        for bad in ("semantic", "KW", "", "hybrid ", "向量"):
            with self.assertRaises(QueryError):
                validate_query("甲", mode=bad)

    def test_defaults_when_omitted(self):
        self.assertEqual(validate_query("甲", None, None, "keyword")[1:], (10, 0, "keyword"))


class StableOrderTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(index=False)
        self.addCleanup(self.h.teardown)

    def test_ties_break_by_material_then_chunk_index(self):
        body = "关键词出现在这里。" * 10
        mid1, _c1 = insert_material(self.h, body, title="甲材料")
        mid2, _c2 = insert_material(self.h, body, title="乙材料")
        result = self.h.retrieve(1, "关键词", mode="keyword", limit=20)
        keys = [(h.material_id, h.chunk_index) for h in result.hits]
        self.assertEqual(keys, sorted(keys))
        self.assertTrue({mid1, mid2} <= {h.material_id for h in result.hits})

    def test_repeated_searches_return_the_same_order(self):
        insert_material(self.h, "关键词出现在这里，并且足够长以形成独立切片。" * 4)
        first = [h.chunk_id for h in self.h.retrieve(1, "关键词", mode="keyword", limit=20).hits]
        second = [h.chunk_id for h in self.h.retrieve(1, "关键词", mode="keyword", limit=20).hits]
        self.assertEqual(first, second)

    def test_higher_hit_count_ranks_first(self):
        """命中次数是首要排序键：多命中排在少命中之前。"""
        insert_material(self.h, "关键词关键词关键词。这里是命中三次的那一份材料。", title="多命中")
        insert_material(self.h, "关键词只出现一次，这里是命中一次的那一份材料。", title="少命中")
        window, total = self.h.search_chunks(1, "关键词")
        self.assertEqual(total, 2)
        counts = [w[0] for w in window]
        self.assertEqual(counts, sorted(counts, reverse=True))
        self.assertGreater(counts[0], counts[1])

    def test_offset_and_limit_slice_the_same_ranking(self):
        """分页是同一份排名上的切片，不是另一套排序。"""
        for i in range(3):
            insert_material(self.h, f"关键词出现在第 {i} 份材料里，其余内容用于填充。",
                            title=f"分页材料{i}")
        full = [h.chunk_id for h in self.h.retrieve(1, "关键词", mode="keyword", limit=20).hits]
        page = [h.chunk_id for h in self.h.retrieve(1, "关键词", mode="keyword", limit=2,
                                                   offset=1).hits]
        self.assertGreater(len(full), 2)
        self.assertEqual(page, full[1:3])


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(index=False)
        self.addCleanup(self.h.teardown)

    def test_ascii_matching_is_case_insensitive(self):
        insert_material(self.h, "Photosynthesis 是光合作用的过程。\n\n另一段没有那个词。")
        for q in ("photosynthesis", "PHOTOSYNTHESIS", "PhotoSynthesis"):
            _window, total = self.h.search_chunks(1, q)
            self.assertEqual(total, 1, q)

    def test_sql_wildcards_are_literal_not_wildcards(self):
        insert_material(self.h, "折扣 100% 的说明写在通知里。\n\n这里没有任何百分号。")
        self.assertEqual(self.h.search_chunks(1, "%")[1], 1)
        self.assertEqual(self.h.search_chunks(1, "_")[1], 0)

    def test_query_is_not_evaluated_as_regex(self):
        insert_material(self.h, "abc 与 def 都在这一段里。")
        self.assertEqual(self.h.search_chunks(1, "a.c")[1], 0)

    def test_chinese_literal_substring_matches(self):
        insert_material(self.h, "倒叙的作用是设置悬念，插叙用于补充背景。")
        self.assertEqual(self.h.search_chunks(1, "倒叙的作用")[1], 1)


class HitFieldsTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(index=True)
        self.addCleanup(self.h.teardown)

    def test_contract_field_set(self):
        hit_dict = self.h.retrieve(1, PHRASE, mode="keyword", limit=1).hits[0].to_dict()
        for field in ("chunk_id", "material_id", "material_title", "chunk_index",
                      "start_offset", "end_offset", "snippet", "score_keyword", "score_vector",
                      "rank_keyword", "rank_vector", "score_final"):
            self.assertIn(field, hit_dict)

    def test_snippet_comes_from_relation_db_and_matches_offsets(self):
        hit = self.h.retrieve(1, PHRASE, mode="keyword", limit=1).hits[0]
        row = self.h.chunk_row(hit.chunk_id, 1)
        self.assertEqual(hit.snippet, row["body_text"])
        with self.h.ctx():
            body = "\n\n".join(
                r["body_text"] for r in db.list_knowledge_entries(row["material_id"], 1)
            )
        cfg = ChunkConfig(strategy="custom", max_chars=150, overlap_percent=0)
        self.assertEqual(preprocess(body, cfg)[hit.start_offset:hit.end_offset], hit.snippet)

    def test_legacy_and_citation_views(self):
        hit = Hit(chunk_id=3, material_id=7, material_title="题", chunk_index=1,
                  start_offset=2, end_offset=9, snippet="片段")
        legacy = hit.to_legacy_dict()
        self.assertEqual(legacy["char_start"], 2)
        self.assertEqual(legacy["source_url"], "/api/knowledge/sources/3")
        self.assertEqual(hit.to_citation(),
                         {"chunk_id": 3, "material_title": "题", "chunk_index": 1,
                          "start_offset": 2, "end_offset": 9, "snippet": "片段"})

    def test_result_status_and_message(self):
        empty = self.h.retrieve(1, UNRELATED, mode="keyword", limit=5)
        self.assertEqual(empty.status, "no_evidence")
        self.assertEqual(empty.to_dict()["message"], NO_EVIDENCE_MESSAGE)


class CoordinateSystemTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(index=True)
        self.addCleanup(self.h.teardown)
        client = self.h.client()
        self.h.login(client, "teacher_a")
        self.body = client.get(f"/api/knowledge/search?q={PHRASE}&limit=3").get_json()

    def test_response_declares_coordinate_system(self):
        coord = self.body["coordinate_system"]
        self.assertEqual(coord["unit"], "unicode_code_point")
        self.assertEqual(coord["normalization"], "NFC")
        self.assertEqual(coord["offset_basis"], "preprocessed_text")
        self.assertEqual(coord["snippet_object"], "knowledge_chunk.body_text")

    def test_offsets_are_reproducible_on_preprocessed_text(self):
        self.assertTrue(self.body["results"])
        for item in self.body["results"]:
            row = self.h.chunk_row(item["chunk_id"], 1)
            self.assertIsNotNone(row)
            self.assertEqual(item["snippet"], row["body_text"])
            self.assertEqual(item["char_end"] - item["char_start"], len(item["snippet"]))

    def test_line_numbers_are_ordered(self):
        for item in self.body["results"]:
            self.assertGreaterEqual(item["line_start"], 1)
            self.assertLessEqual(item["line_start"], item["line_end"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
