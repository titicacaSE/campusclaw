"""问答：先检索再生成（design.md §6）

规约依据 `add-traceable-vector-retrieval`：
  - `POST /api/ask` 以 **hybrid** 模式取回**本班前 4 条**切片（top_k = 4）
  - **0 条则不调用对话网关**，直接返回固定文案（D12）
  - 上下文与 `system` 提示**由服务端组装**；客户端 `history` 里的 `system` 消息一律丢弃（D13）
  - 上下文不含向量分量、不含 Qdrant 原始点数据、不含其他班级切片
  - `answer` 中的 `[n]` 与 `citations[]` 下标顺序一致；越界引用不产生 citation 且记 warning
  - 对话网关失败 → `upstream_error`（接口层转 502），**不返回本地拼凑的答案**
"""
from __future__ import annotations

import logging
import re

from . import gateway
from .retrieval import NO_EVIDENCE_MESSAGE, RetrievalResult, Hit, retrieve

log = logging.getLogger("campusclaw.qa")

TOP_K_CONTEXT = 4                      # 课件口径：本班前 4 条
MAX_HISTORY_TURNS = 20                 # 只附最近若干轮，避免上下文无限膨胀
HISTORY_ROLES = ("user", "assistant")

# system 提示由服务端写入，客户端无法替换或追加（D13）
SYSTEM_PROMPT = (
    "你是班级知识库助手。**只允许依据下面带编号的资料作答**，"
    "不得依据你自己的记忆、常识或猜测补充任何事实。"
    "若资料不足以回答，直接回答「资料中未找到相关内容」。"
    "回答必须简短，并在引用处标注编号，格式为 [1]、[2]，编号与资料序号一一对应。"
)

_CITATION_RE = re.compile(r"\[(\d{1,3})\]")


# ---------------------------------------------------------------- 上下文组装


def _context_block(hits: list[Hit]) -> str:
    """只含材料标题、切片序号、切片正文 —— 不含向量分量、不含 Qdrant 原始点数据。"""
    lines = ["以下是本班知识库中检索到的资料，编号即引用编号："]
    for idx, hit in enumerate(hits, start=1):
        lines.append(
            f"\n[{idx}] 材料《{hit.material_title}》第 {hit.chunk_index} 段：\n{hit.snippet}"
        )
    return "\n".join(lines)


def clean_history(history) -> list[dict]:
    """丢弃客户端 `system` 消息，只保留 user/assistant 的文本轮次。"""
    if not isinstance(history, list):
        return []
    cleaned: list[dict] = []
    dropped_system = 0
    for turn in history:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "").strip().lower()
        if role == "system":
            dropped_system += 1
            continue
        if role not in HISTORY_ROLES:
            continue
        content = turn.get("content")
        if not isinstance(content, str) or not content:
            continue
        cleaned.append({"role": role, "content": content})
    if dropped_system:
        log.warning("丢弃客户端 system 消息 %d 条（system 提示由服务端写入）", dropped_system)
    return cleaned[-MAX_HISTORY_TURNS:]


def build_messages(question: str, hits: list[Hit], history=None) -> list[dict]:
    """组装发给对话网关的消息序列。**只有服务端能构造这段内容。**"""
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if hits:
        messages.append({"role": "system", "content": _context_block(hits)})
    messages.extend(clean_history(history))
    messages.append({"role": "user", "content": question})
    return messages


def referenced_indices(answer: str, available: int) -> tuple[list[int], list[int]]:
    """解析 `answer` 中的 `[n]`。返回 `(合法编号去重升序, 越界编号去重升序)`。"""
    seen: list[int] = []
    invalid: list[int] = []
    for raw in _CITATION_RE.findall(answer or ""):
        n = int(raw)
        target = invalid if (n < 1 or n > available) else seen
        if n not in target:
            target.append(n)
    return sorted(seen), sorted(invalid)


def build_citations(hits: list[Hit]) -> list[dict]:
    """citation 列表顺序 = 传给模型的切片顺序（`[1]` ↔ `citations[0]`）。"""
    return [hit.to_citation() for hit in hits]


# ---------------------------------------------------------------- 主入口


class AnswerResult(dict):
    """便于直接 jsonify。"""


def answer(
    class_id: int,
    question: str,
    history=None,
    *,
    qdrant=None,
    cfg: gateway.GatewayConfig | None = None,
    embed_transport=None,
    chat_transport=None,
    retrieval: RetrievalResult | None = None,
) -> AnswerResult:
    """执行「先检索，再生成」。

    无命中时**不会触碰对话网关** —— 这是本变更最核心的一条纪律。
    """
    cfg = cfg or gateway.GatewayConfig.from_app()

    result = retrieval or retrieve(
        class_id,
        question,
        mode="hybrid",
        limit=TOP_K_CONTEXT,
        offset=0,
        qdrant=qdrant,
        cfg=cfg,
        embed_transport=embed_transport,
    )
    hits = result.hits[:TOP_K_CONTEXT]

    payload: AnswerResult = AnswerResult(
        answer=NO_EVIDENCE_MESSAGE,
        status="no_evidence",
        message=NO_EVIDENCE_MESSAGE,      # 无命中时状态与文案同时给出（规约 T6）
        citations=[],
        question=question,
        degraded=result.degraded,
        index_warnings=result.index_warnings,
    )

    if not hits:
        # 刻意不调用对话网关：让模型凭参数记忆勉强作答正是课件否决的做法
        log.info("问答无命中，未调用对话网关（class_id=%s）", class_id)
        return payload

    messages = build_messages(question, hits, history)
    text = gateway.chat(messages, cfg=cfg, transport=chat_transport)   # 失败抛 ChatUnavailable

    referenced, out_of_range = referenced_indices(text, len(hits))
    if out_of_range:
        log.warning("模型引用了越界编号 %s（本次仅传入 %d 条切片）", out_of_range, len(hits))

    payload["answer"] = text
    payload["status"] = "ok"
    payload["citations"] = build_citations(hits)
    payload["referenced"] = referenced
    payload["out_of_range_refs"] = out_of_range
    return payload
