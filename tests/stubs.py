"""测试替身：伪嵌入/对话网关 + Qdrant 内存库（离线可跑，零第三方依赖）

为什么需要「伪嵌入」
--------------------
本机没有课程网关凭据，也没有 Docker（跑不起真实 Qdrant）。嵌入网关是**外部依赖**，
验收时不能真的联网，否则用例不可重复。于是用一个**确定性、可解释**的替身：

  - 文本 → 「相邻两字（bigram）」的出现计数向量，L2 归一化，维度 = `EMBEDDING_DIM`
  - 因此：**文本高度重叠 → 余弦高；毫无共同片段 → 余弦≈0**

它与真实语义嵌入的差别是**明确的**（它只认字面重叠，不懂同义改写），所以：
  - 需要「同义不同词也能召回」的判据 **不应** 依赖它
  - 需要「向量路是否接上、班级过滤是否前置、阈值是否生效」的判据可以用它
  - `ACCEPTANCE.md` 里按「替身验证 / 真实 Qdrant + 真实网关待复核」分别标注

调用日志（`embed_calls` / `chat_calls`）是本替身最重要的部分：
「页面上出现文字」不等于「模型被调用过」——无命中用例就是靠 `chat_calls == 0` 判定的。
"""
from __future__ import annotations

import hashlib
import math

DEFAULT_DIM = 512


# ---------------------------------------------------------------- 伪嵌入


def _bigrams(text: str) -> list[str]:
    return [text[i:i + 2] for i in range(max(0, len(text) - 1))]


def pseudo_embedding(text: str, dim: int = DEFAULT_DIM) -> list[float]:
    """确定性伪嵌入：bigram 计数 → 哈希投影 → L2 归一化。

    相同输入永远得到相同向量；空文本得到全零向量（调用方不应据此判定命中）。
    """
    vector = [0.0] * int(dim)
    for gram in _bigrams(text or ""):
        bucket = int(hashlib.md5(gram.encode("utf-8")).hexdigest(), 16) % int(dim)
        vector[bucket] += 1.0
    norm = math.sqrt(sum(x * x for x in vector))
    if norm:
        vector = [x / norm for x in vector]
    return vector


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# ---------------------------------------------------------------- 伪网关


class GatewayError(RuntimeError):
    pass


class FakeGateway:
    """伪课程网关。同时提供嵌入与对话两个传输层函数，并记录每一次调用。"""

    def __init__(self, dim: int = DEFAULT_DIM, *, embed_down: bool = False,
                 chat_down: bool = False, dim_mismatch: bool = False,
                 answer: str | None = None):
        self.dim = int(dim)
        self.embed_down = embed_down
        self.chat_down = chat_down
        self.dim_mismatch = dim_mismatch
        self.answer = answer or "根据本班资料：倒叙用于设置悬念，插叙用于补充背景 [1]。"
        self.embed_calls: list[dict] = []
        self.chat_calls: list[dict] = []

    # -------------------------------------------------- 传输层
    def embed_transport(self, url: str, headers: dict, payload: dict, timeout: int) -> dict:
        self.embed_calls.append({"url": url, "headers": dict(headers), "payload": payload})
        if self.embed_down:
            raise GatewayError("伪嵌入网关注入失败")
        dim = self.dim + 1 if self.dim_mismatch else self.dim
        return {
            "data": [{"embedding": pseudo_embedding(t, dim)} for t in (payload.get("input") or [])],
            "model": payload.get("model"),
        }

    def chat_transport(self, url: str, headers: dict, payload: dict, timeout: int) -> dict:
        self.chat_calls.append({"url": url, "headers": dict(headers), "payload": payload})
        if self.chat_down:
            raise GatewayError("伪对话网关注入失败")
        return {"choices": [{"message": {"role": "assistant", "content": self.answer}}]}

    # -------------------------------------------------- 断言辅助
    @property
    def embed_query_texts(self) -> list[list[str]]:
        return [list((c["payload"] or {}).get("input") or []) for c in self.embed_calls]

    def embedding_requests_for(self, text: str) -> int:
        """该文本被单独送去嵌入过几次（用于「不重复嵌入」的幂等断言）。"""
        return sum(1 for batch in self.embed_query_texts for item in batch if item == text)

    def reset(self) -> None:
        self.embed_calls.clear()
        self.chat_calls.clear()

    def redacted_ok(self, blob: str) -> bool:
        """密钥是否从未出现在给定文本中（no-secret 用例用）。"""
        return not any(
            value in blob
            for call in self.embed_calls + self.chat_calls
            for value in ((call["headers"] or {}).get("Authorization", ""),)
            if value
        )
