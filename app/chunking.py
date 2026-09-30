"""切片与预处理（design.md §3）

规约依据 `add-traceable-vector-retrieval`：
  - 三种切分策略 `auto` / `custom` / `hierarchy`，默认 `auto`（D5）
  - 预处理 MUST NOT 改写原文；切片偏移相对**预处理后文本**（D7）
  - 偏移为 Unicode 码点、左闭右开；行号 1 起、右闭（D6）
  - 同一正文 + 同一参数重复切分 MUST 得到完全相同的划分与坐标（确定性）

本模块不接触数据库、不接触网络，可脱离 Flask 上下文单独测试。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from bisect import bisect_right
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 常量与参数域

STRATEGIES = ("auto", "custom", "hierarchy")

DEFAULT_MAX_CHARS = 800          # auto 默认最大长度
DEFAULT_OVERLAP_PERCENT = 10     # auto 默认重叠 10%
MIN_MAX_CHARS = 100              # custom 下界
MAX_MAX_CHARS = 2000             # custom 上界
MIN_OVERLAP_PERCENT = 0
MAX_OVERLAP_PERCENT = 50
MIN_MERGE_CHARS = 32             # 过短合并阈值

SENTENCE_ENDERS = "。！？.!?"
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# 边界优先级：空行 > 换行 > 句号
RANK_PARA = 3
RANK_LINE = 2
RANK_SENTENCE = 1

_MISSING = object()


class ChunkError(ValueError):
    """切分配置非法。接口层据此返回 400 invalid_query。"""


# ---------------------------------------------------------------- 配置


@dataclass
class ChunkConfig:
    """切分参数。字段名与课件接口契约的请求字段一一对应。"""

    strategy: str = "auto"
    separator: str | None = None
    max_chars: int = DEFAULT_MAX_CHARS
    overlap_percent: int = DEFAULT_OVERLAP_PERCENT
    collapse_whitespace: bool = False
    strip_links: bool = False

    # -------------------------------------------------- 校验与构造

    def validate(self, *, for_custom: bool | None = None) -> "ChunkConfig":
        """校验参数域。越界抛 ChunkError（**不静默夹取**，规约 Scenario: custom 参数越界被拒绝）。"""
        if self.strategy not in STRATEGIES:
            raise ChunkError(f"chunk_strategy 必须是 {'/'.join(STRATEGIES)} 之一")
        strict = for_custom if for_custom is not None else (self.strategy == "custom")
        if strict:
            if not (MIN_MAX_CHARS <= int(self.max_chars) <= MAX_MAX_CHARS):
                raise ChunkError(
                    f"chunk_max_chars 必须在 {MIN_MAX_CHARS}..{MAX_MAX_CHARS} 之间"
                )
            if not (MIN_OVERLAP_PERCENT <= int(self.overlap_percent) <= MAX_OVERLAP_PERCENT):
                raise ChunkError(
                    f"chunk_overlap_percent 必须在 {MIN_OVERLAP_PERCENT}..{MAX_OVERLAP_PERCENT} 之间"
                )
        else:
            if int(self.max_chars) <= 0:
                raise ChunkError("chunk_max_chars 必须为正整数")
            if int(self.overlap_percent) < 0:
                raise ChunkError("chunk_overlap_percent 不能为负")
        return self

    @property
    def max_chars_int(self) -> int:
        return max(1, int(self.max_chars))

    @property
    def overlap(self) -> int:
        """重叠码点数 = max_chars × overlap_percent / 100（向下取整）。

        重叠 MUST 小于块长，否则退化为 max_chars // 5（design.md §3.2）。
        """
        ov = self.max_chars_int * int(self.overlap_percent) // 100
        if ov >= self.max_chars_int:
            ov = max(0, self.max_chars_int // 5)
        return ov

    def as_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "separator": self.separator,
            "max_chars": self.max_chars_int,
            "overlap_percent": int(self.overlap_percent),
            "collapse_whitespace": bool(self.collapse_whitespace),
            "strip_links": bool(self.strip_links),
        }

    @classmethod
    def from_request(cls, payload: dict | None) -> "ChunkConfig":
        """从请求体/表单构造。缺省全部用默认值；strategy=custom 时校验参数域。"""
        payload = payload or {}

        def pick(*names, default=_MISSING):
            for n in names:
                if n in payload and payload[n] not in (None, ""):
                    return payload[n]
            return default

        strategy = str(pick("chunk_strategy", "strategy", default="auto")).strip() or "auto"
        if strategy not in STRATEGIES:
            raise ChunkError(f"chunk_strategy 必须是 {'/'.join(STRATEGIES)} 之一")

        def to_int(value, default, label):
            if value is _MISSING or value is None:
                return default
            try:
                return int(str(value).strip())
            except (TypeError, ValueError) as exc:
                raise ChunkError(f"{label} 必须是整数") from exc

        def to_bool(value, default):
            if value is _MISSING or value is None:
                return default
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on")

        cfg = cls(
            strategy=strategy,
            separator=pick("chunk_separator", "separator", default=None),
            max_chars=to_int(pick("chunk_max_chars", "max_chars"), DEFAULT_MAX_CHARS, "chunk_max_chars"),
            overlap_percent=to_int(
                pick("chunk_overlap_percent", "overlap_percent"),
                DEFAULT_OVERLAP_PERCENT,
                "chunk_overlap_percent",
            ),
            collapse_whitespace=to_bool(
                pick("collapse_whitespace"), False
            ),
            strip_links=to_bool(pick("strip_links"), False),
        )
        # 参数域纪律：**只要客户端显式给了数值参数，就按 custom 的边界校验**，
        # 而不是看它有没有记得同时声明 strategy=custom。否则 `chunk_max_chars=5`
        # 配合默认 strategy=auto 会被悄悄接受（静默夹取），与实际生效值不符。
        explicit_numbers = any(
            key in payload and payload[key] not in (None, "")
            for key in ("chunk_max_chars", "max_chars", "chunk_overlap_percent", "overlap_percent")
        )
        return cfg.validate(for_custom=(strategy == "custom") or explicit_numbers)


# ---------------------------------------------------------------- 预处理


def normalize_newlines(text: str) -> str:
    """CRLF / 孤立 CR → LF。行号在归一化之后计算（规约 Scenario: CRLF 与 LF 行号一致）。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def collapse_whitespace(text: str) -> str:
    """连续空白折叠为单个空格，**但保留 `\\n`**（否则行号失去意义）。"""
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)          # 去掉行首行尾多余空格
    return re.sub(r"\n{3,}", "\n\n", text)        # 超长空行折叠为段落分隔


def strip_links(text: str) -> str:
    """移除 URL 与邮箱。"""
    return EMAIL_RE.sub(" ", URL_RE.sub(" ", text))


def preprocess(text: str, cfg: ChunkConfig | None = None) -> str:
    """预处理 → 作为切片坐标系。**不改写原文**：只产出索引侧视图。"""
    cfg = cfg or ChunkConfig()
    out = normalize_newlines(text)
    if cfg.strip_links:
        out = strip_links(out)
    if cfg.collapse_whitespace:
        out = collapse_whitespace(out)
    return unicodedata.normalize("NFC", out)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 切片


@dataclass
class Chunk:
    chunk_index: int
    body_text: str
    start_offset: int          # 相对预处理后文本的码点偏移（含）
    end_offset: int            # 相对预处理后文本的码点偏移（不含）
    line_start: int            # 1 起
    line_end: int              # 1 起，含末行
    section: str | None = None  # hierarchy 策略下的章节路径

    @property
    def char_count(self) -> int:
        return len(self.body_text)

    @property
    def chunk_sha256(self) -> str:
        return sha256_text(self.body_text)


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _line_of(starts: list[int], offset: int) -> int:
    """码点偏移 → 1 起行号。"""
    if offset < 0:
        return 1
    return bisect_right(starts, offset)


def _boundary_ranks(text: str) -> dict[int, int]:
    """边界表：offset → 优先级（在该 offset **之前**断开）。"""
    ranks: dict[int, int] = {}
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch == "\n":
            j = i
            while j < n and text[j] == "\n":
                j += 1
            rank = RANK_PARA if (j - i) >= 2 else RANK_LINE
            if ranks.get(j, 0) < rank:
                ranks[j] = rank
            i = j
            continue
        if ch in SENTENCE_ENDERS:
            end = i + 1
            # 句末可能紧跟引号/括号，一并并入边界
            while end < n and text[end] in "”’）)】」』\"'":
                end += 1
            if ranks.get(end, 0) < RANK_SENTENCE:
                ranks[end] = RANK_SENTENCE
            i = end
            continue
        i += 1
    return ranks


def _greedy_pieces(text: str, cfg: ChunkConfig) -> list[tuple[int, int]]:
    """贪心切分：在候选窗口内取「优先级最高、同级取最靠后」的边界，无边界则硬切。"""
    max_chars, overlap = cfg.max_chars_int, cfg.overlap
    ranks = _boundary_ranks(text)
    n = len(text)
    pieces: list[tuple[int, int]] = []
    pos = 0

    while pos < n:
        ideal = pos + max_chars
        if ideal >= n:
            pieces.append((pos, n))
            break
        floor_off = pos + max_chars // 2      # 避免切出过短的片
        best_off, best_rank = None, 0
        for off in range(floor_off, ideal + 1):
            rank = ranks.get(off, 0)
            if rank and rank >= best_rank:
                best_rank, best_off = rank, off
        cut = best_off if best_off is not None else ideal
        pieces.append((pos, cut))
        nxt = cut - overlap
        pos = nxt if nxt > pos else cut       # 保证前进，不会死循环

    return pieces


def _merge_short(pieces: list[tuple[int, int]], text: str, cfg: ChunkConfig) -> list[tuple[int, int]]:
    """过短合并：相邻两片合并后 ≤ max_chars 且其中一片 < MIN_MERGE_CHARS。"""
    if len(pieces) <= 1:
        return pieces
    max_chars = cfg.max_chars_int
    out = [pieces[0]]
    for start, end in pieces[1:]:
        p_start, p_end = out[-1]
        prev_len, cur_len = p_end - p_start, end - start
        if (prev_len < MIN_MERGE_CHARS or cur_len < MIN_MERGE_CHARS) and (end - p_start) <= max_chars:
            out[-1] = (p_start, end)
        else:
            out.append((start, end))
    return out


def _split_by_separator(text: str, separator: str) -> list[tuple[int, int]]:
    if not separator:
        separator = "\n\n"
    pieces: list[tuple[int, int]] = []
    pos = 0
    while True:
        idx = text.find(separator, pos)
        if idx < 0:
            if pos < len(text):
                pieces.append((pos, len(text)))
            break
        if idx > pos:
            pieces.append((pos, idx))
        pos = idx + len(separator)
    return pieces or [(0, len(text))]


def _split_hierarchy(text: str, cfg: ChunkConfig) -> list[tuple[int, int, str]]:
    """按 `#` / `##` / `###` 分章；单章过长再按窗口切。返回 (start, end, section)。"""
    head_re = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.M)
    marks = [(m.start(), m.group(1), m.group(2)) for m in head_re.finditer(text)]
    sections: list[tuple[int, int, str]] = []

    if not marks:
        sections.append((0, len(text), ""))
    else:
        if marks[0][0] > 0:
            sections.append((0, marks[0][0], ""))
        stack: list[tuple[int, str]] = []
        for idx, (off, hashes, title) in enumerate(marks):
            level = len(hashes)
            stack = [(lv, t) for lv, t in stack if lv < level]
            stack.append((level, title))
            path = " / ".join(t for _, t in stack)
            end = marks[idx + 1][0] if idx + 1 < len(marks) else len(text)
            sections.append((off, end, path))

    pieces: list[tuple[int, int, str]] = []
    for start, end, path in sections:
        seg = text[start:end]
        if len(seg) <= cfg.max_chars_int:
            if seg.strip():
                pieces.append((start, end, path))
            continue
        for sub_start, sub_end in _greedy_pieces(seg, cfg):
            if text[start + sub_start:start + sub_end].strip():
                pieces.append((start + sub_start, start + sub_end, path))
    return pieces


def chunk_text(text: str, cfg: ChunkConfig | None = None) -> list[Chunk]:
    """把正文切成可定位的切片。`text` 会被先预处理，偏移相对预处理后文本。"""
    cfg = (cfg or ChunkConfig()).validate(for_custom=(cfg or ChunkConfig()).strategy == "custom")
    body = preprocess(text, cfg)
    if not body.strip():
        return []

    if cfg.strategy == "hierarchy":
        raw = _split_hierarchy(body, cfg)
        triples = [(s, e, sec) for s, e, sec in raw]
    else:
        if cfg.strategy == "custom":
            base = _split_by_separator(body, cfg.separator or "")
            expanded: list[tuple[int, int]] = []
            for s, e in base:
                if (e - s) <= cfg.max_chars_int:
                    if body[s:e].strip():
                        expanded.append((s, e))
                    continue
                for a, b in _greedy_pieces(body[s:e], cfg):
                    expanded.append((s + a, s + b))
            pairs = expanded or [(0, len(body))]
        else:  # auto
            pairs = _greedy_pieces(body, cfg)
        pairs = _merge_short(pairs, body, cfg)
        triples = [(s, e, None) for s, e in pairs]

    starts = _line_starts(body)
    chunks: list[Chunk] = []
    for idx, (start, end, section) in enumerate(triples):
        seg = body[start:end]
        if not seg.strip():
            continue
        chunks.append(
            Chunk(
                chunk_index=len(chunks),
                body_text=seg,
                start_offset=start,
                end_offset=end,
                line_start=_line_of(starts, start),
                line_end=_line_of(starts, max(start, end - 1)),
                section=section,
            )
        )
    return chunks
