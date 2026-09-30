"""切片与预处理单元测试（T3）

覆盖 tasks.md 的类名：PreprocessTests / AutoStrategyTests / CustomStrategyTests /
HierarchyStrategyTests / ChunkOffsetTests / BoundaryTests / DeterminismTests。

这些测试不碰数据库、不碰网络 —— `app/chunking.py` 是纯函数模块，
因此可以在毫秒级验完坐标系的每一条纪律。
"""
from __future__ import annotations

import sys
import unicodedata
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.chunking import (  # noqa: E402
    MAX_MAX_CHARS,
    MAX_OVERLAP_PERCENT,
    MIN_MAX_CHARS,
    ChunkConfig,
    ChunkError,
    chunk_text,
    collapse_whitespace,
    normalize_newlines,
    preprocess,
    sha256_text,
    strip_links,
)
from app.retrieval import build_chunk_records  # noqa: E402

PARA = (
    "A 班语文讲义 · 第 1 讲：记叙文阅读方法\n\n"
    "一、要素梳理。阅读记叙文先抓六要素：时间、地点、人物、起因、经过、结果。"
    "先用一句话概括事件，再进入细节分析。\n\n"
    "二、线索与顺序。常见线索有人物、事件、物品、情感；顺序分为顺叙、倒叙、插叙。"
    "倒叙的作用是设置悬念，插叙的作用是补充背景、丰满人物。\n\n"
    "三、人物形象。从正面描写与侧面描写两条路径归纳人物性格，答题时用"
    "「特征 + 依据」的结构，做到有据可依。\n"
)


class PreprocessTests(unittest.TestCase):
    def test_crlf_and_lone_cr_become_lf(self):
        self.assertEqual(normalize_newlines("a\r\nb\rc\nd"), "a\nb\nc\nd")
        self.assertEqual(preprocess("a\r\nb"), "a\nb")

    def test_strip_links_removes_url_and_email(self):
        out = strip_links("见 https://example.com/x?y=1 或 mailto a.b@c.com 联系我们")
        self.assertNotIn("http", out)
        self.assertNotIn("a.b@c.com", out)
        self.assertIn("联系我们", out)

    def test_collapse_whitespace_keeps_newlines(self):
        out = collapse_whitespace("a   b\t\tc\n\n\n\nd   \n   e")
        self.assertIn("\n", out)
        self.assertNotIn("\t", out)
        self.assertNotIn("   ", out)
        self.assertIn("d\ne", out)

    def test_nfc_normalization(self):
        decomposed = "e\u0301"          # e + 组合尖音符
        self.assertEqual(preprocess(decomposed), unicodedata.normalize("NFC", decomposed))
        self.assertEqual(len(preprocess(decomposed)), 1)

    def test_preprocess_is_pure_and_does_not_rewrite_simple_text(self):
        text = "已是规范文本\n第二行"
        self.assertEqual(preprocess(text), text)      # 无声明变换时不做任何改写
        self.assertEqual(text, "已是规范文本\n第二行")  # 入参本身不可被修改

    def test_optional_transforms_are_opt_in(self):
        text = "a  b http://x.y  c\n\n\n\n\nd"
        plain = preprocess(text, ChunkConfig())
        self.assertIn("http", plain)
        self.assertIn("a  b", plain)
        folded = preprocess(text, ChunkConfig(collapse_whitespace=True, strip_links=True))
        self.assertNotIn("http", folded)
        self.assertIn("a b", folded)


class AutoStrategyTests(unittest.TestCase):
    def test_defaults_are_800_and_10_percent(self):
        cfg = ChunkConfig()
        self.assertEqual(cfg.max_chars_int, 800)
        self.assertEqual(cfg.overlap_percent, 10)
        self.assertEqual(cfg.overlap, 80)

    def test_prefers_paragraph_then_line_then_sentence(self):
        """边界优先级：空行(3) > 换行(2) > 句号(1)；同级取最靠后；窗口内无边界才硬切。

        用构造文本把偏移算准，避免依赖语料细节：
          甲×10 | \\n | 乙×10 | \\n\\n | 丙×10
          偏移：        11(换行)          23(空行)
        """
        text = "甲" * 10 + "\n" + "乙" * 10 + "\n\n" + "丙" * 10
        self.assertEqual(text[10], "\n")
        self.assertEqual(text[21:23], "\n\n")

        # 窗口 [7,15] 只看到换行边界(11) → 切在 11
        chunks = chunk_text(text, ChunkConfig(strategy="auto", max_chars=15, overlap_percent=0))
        self.assertEqual(chunks[0].end_offset, 11)
        self.assertEqual(chunks[0].body_text.rstrip("\n"), "甲" * 10)

        # 窗口 [12,25] 里空行边界(23) 优先级更高 → 切在 23
        chunks = chunk_text(text, ChunkConfig(strategy="auto", max_chars=25, overlap_percent=0))
        self.assertEqual(chunks[0].end_offset, 23)
        self.assertEqual(chunks[0].body_text.rstrip("\n"), "甲" * 10 + "\n" + "乙" * 10)

    def test_sentence_boundary_preferred_over_hard_cut(self):
        text = "甲" * 10 + "。" + "乙" * 10 + "。" + "丙" * 10
        chunks = chunk_text(text, ChunkConfig(strategy="auto", max_chars=15, overlap_percent=0))
        self.assertEqual(chunks[0].end_offset, 11)
        self.assertTrue(chunks[0].body_text.endswith("。"))

    def test_hard_cut_when_no_boundary_exists(self):
        text = "甲" * 500
        chunks = chunk_text(text, ChunkConfig(strategy="auto", max_chars=100, overlap_percent=0))
        self.assertTrue(all(c.char_count <= 100 for c in chunks))
        self.assertEqual("".join(c.body_text for c in chunks), text)
        self.assertEqual(len(chunks), 5)

    def test_overlap_shares_codepoints(self):
        text = "。".join(f"第{i}句内容" for i in range(200))
        cfg = ChunkConfig(strategy="auto", max_chars=300, overlap_percent=10)
        chunks = chunk_text(text, cfg)
        self.assertGreater(len(chunks), 2)
        prev_end = chunks[0].end_offset
        self.assertEqual(chunks[1].start_offset, prev_end - cfg.overlap)
        self.assertLess(cfg.overlap, cfg.max_chars_int)

    def test_empty_or_whitespace_only_returns_nothing(self):
        self.assertEqual(chunk_text("", ChunkConfig()), [])
        self.assertEqual(chunk_text("   \n\n\t  ", ChunkConfig()), [])


class CustomStrategyTests(unittest.TestCase):
    def test_bounds_are_enforced_not_clamped(self):
        for bad in (MIN_MAX_CHARS - 1, MAX_MAX_CHARS + 1, 0, -5):
            with self.assertRaises(ChunkError, msg=f"max_chars={bad} 应被拒绝"):
                ChunkConfig(strategy="custom", max_chars=bad).validate(for_custom=True)
        for bad in (MAX_OVERLAP_PERCENT + 1, -1):
            with self.assertRaises(ChunkError, msg=f"overlap={bad} 应被拒绝"):
                ChunkConfig(strategy="custom", overlap_percent=bad).validate(for_custom=True)

    def test_boundary_values_are_accepted(self):
        for good in (MIN_MAX_CHARS, MAX_MAX_CHARS):
            ChunkConfig(strategy="custom", max_chars=good).validate(for_custom=True)
        for good in (0, MAX_OVERLAP_PERCENT):
            ChunkConfig(strategy="custom", overlap_percent=good).validate(for_custom=True)

    def test_from_request_enforces_bounds_even_without_declaring_custom(self):
        """显式给出数值参数就按 custom 的边界校验 —— 否则 auto + max_chars=5 会被静默接受。"""
        with self.assertRaises(ChunkError):
            ChunkConfig.from_request({"chunk_max_chars": 5})
        with self.assertRaises(ChunkError):
            ChunkConfig.from_request({"chunk_overlap_percent": 99})
        cfg = ChunkConfig.from_request({"chunk_strategy": "custom", "chunk_max_chars": 200})
        self.assertEqual(cfg.max_chars_int, 200)

    def test_from_request_defaults_and_unknown_strategy(self):
        cfg = ChunkConfig.from_request({})
        self.assertEqual((cfg.strategy, cfg.max_chars_int, cfg.overlap_percent), ("auto", 800, 10))
        with self.assertRaises(ChunkError):
            ChunkConfig.from_request({"chunk_strategy": "bogus"})

    def test_separator_is_respected(self):
        """自定义分隔符生效。

        注意：每段都长于 MIN_MERGE_CHARS(32) —— 否则会被 design.md §3.2 的
        「过短合并」并回一片（合并是刻意的，避免出现一堆几十字的碎片）。
        """
        pieces = ["段一" * 20, "段二" * 20, "段三" * 20]
        text = "---".join(pieces)
        chunks = chunk_text(
            text, ChunkConfig(strategy="custom", separator="---", max_chars=2000, overlap_percent=0)
        )
        self.assertEqual([c.body_text for c in chunks], pieces)

    def test_short_pieces_are_merged_by_design(self):
        text = "段一内容。---段二内容。---段三内容。"
        chunks = chunk_text(
            text, ChunkConfig(strategy="custom", separator="---", max_chars=2000, overlap_percent=0)
        )
        self.assertEqual(len(chunks), 1)      # 三片都 < 32 码点 → 合并成一片
        self.assertEqual(chunks[0].body_text, text)

    def test_long_piece_is_windowed(self):
        text = "\n\n".join("乙" * 300 for _ in range(3))
        chunks = chunk_text(text, ChunkConfig(strategy="custom", max_chars=150, overlap_percent=0))
        self.assertTrue(all(c.char_count <= 150 for c in chunks))
        self.assertGreaterEqual(len(chunks), 6)


class HierarchyStrategyTests(unittest.TestCase):
    MD = (
        "# 第一章 总论\n"
        "引言正文，说明本章目的与范围。\n\n"
        "## 1.1 定义\n"
        "定义正文，列出关键术语并为后续推导做准备。\n\n"
        "### 1.1.1 记号\n"
        "记号正文，约定符号与下标规则。\n\n"
        "## 1.2 性质\n"
        "性质正文，逐条列出并给出简要说明。\n"
    )

    def test_sections_record_hierarchy_path(self):
        chunks = chunk_text(self.MD, ChunkConfig(strategy="hierarchy", max_chars=2000))
        paths = [c.section for c in chunks]
        self.assertIn("第一章 总论", paths)
        self.assertIn("第一章 总论 / 1.1 定义", paths)
        self.assertIn("第一章 总论 / 1.1 定义 / 1.1.1 记号", paths)
        self.assertTrue(all(c.section for c in chunks), paths)

    def test_long_section_is_split_further_and_keeps_section(self):
        md = "# 长章\n" + ("丙" * 900)
        chunks = chunk_text(md, ChunkConfig(strategy="hierarchy", max_chars=200, overlap_percent=0))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(c.char_count <= 200 for c in chunks))
        self.assertTrue(all(c.section == "长章" for c in chunks))

    def test_text_without_headings_still_chunks(self):
        chunks = chunk_text(PARA, ChunkConfig(strategy="hierarchy", max_chars=2000))
        self.assertTrue(chunks)
        self.assertTrue(all(c.section == "" for c in chunks))


class ChunkOffsetTests(unittest.TestCase):
    def test_offsets_are_codepoints_left_closed_right_open(self):
        cfg = ChunkConfig(strategy="custom", max_chars=150, overlap_percent=0)
        chunks = chunk_text(PARA, cfg)
        body = preprocess(PARA, cfg)
        for c in chunks:
            self.assertEqual(body[c.start_offset:c.end_offset], c.body_text)
            self.assertLess(c.start_offset, c.end_offset)

    def test_line_numbers_are_one_based_and_inclusive(self):
        cfg = ChunkConfig(strategy="custom", max_chars=150, overlap_percent=0)
        chunks = chunk_text(PARA, cfg)
        lines = preprocess(PARA, cfg).split("\n")
        for c in chunks:
            self.assertGreaterEqual(c.line_start, 1)
            self.assertLessEqual(c.line_start, c.line_end)
            self.assertLessEqual(c.line_end, len(lines))

    def test_chunk_sha256_matches_body_text(self):
        for c in chunk_text(PARA, ChunkConfig()):
            self.assertEqual(c.chunk_sha256, sha256_text(c.body_text))

    def test_build_records_carries_source_sha256_of_preprocessed_text(self):
        cfg = ChunkConfig(strategy="custom", max_chars=150, overlap_percent=0)
        records, source_sha = build_chunk_records(7, 1, PARA, cfg)
        self.assertEqual(source_sha, sha256_text(preprocess(PARA, cfg)))
        self.assertEqual([r["chunk_index"] for r in records], list(range(len(records))))
        self.assertTrue(all(r["source_sha256"] == source_sha for r in records))


class BoundaryTests(unittest.TestCase):
    def test_emoji_offsets_stay_consistent(self):
        text = "开始 🎉🎉 中间 🚀 结束。"
        cfg = ChunkConfig(strategy="custom", max_chars=2000)
        body = preprocess(text, cfg)
        chunks = chunk_text(text, cfg)
        for c in chunks:
            self.assertEqual(body[c.start_offset:c.end_offset], c.body_text)
        self.assertEqual(len(body), len(text))       # Python 中 emoji 是单个码点

    def test_combining_characters_are_nfc_normalized(self):
        text = "e\u0301" + "内容" * 30
        cfg = ChunkConfig(strategy="custom", max_chars=2000)
        body = preprocess(text, cfg)
        self.assertEqual(body, unicodedata.normalize("NFC", text))
        for c in chunk_text(text, cfg):
            self.assertEqual(body[c.start_offset:c.end_offset], c.body_text)

    def test_crlf_and_lf_produce_identical_line_numbers(self):
        lf = "第一行\n第二行\n第三行\n第四行\n"
        crlf = lf.replace("\n", "\r\n")
        cfg = ChunkConfig(strategy="custom", max_chars=2000)
        a = chunk_text(lf, cfg)
        b = chunk_text(crlf, cfg)
        self.assertEqual([(c.line_start, c.line_end) for c in a],
                         [(c.line_start, c.line_end) for c in b])
        self.assertEqual([c.body_text for c in a], [c.body_text for c in b])

    def test_very_long_line_without_spaces_is_hard_cut(self):
        text = "丁" * 3000
        chunks = chunk_text(text, ChunkConfig(strategy="auto", max_chars=800, overlap_percent=0))
        self.assertTrue(all(c.char_count <= 800 for c in chunks))
        self.assertEqual(sum(c.char_count for c in chunks), 3000)

    def test_whitespace_only_body_has_no_chunks(self):
        for body in ("", " ", "\n\n\n", "\t\r\n  "):
            self.assertEqual(chunk_text(body, ChunkConfig()), [], repr(body))

    def test_text_spanning_a_cut_is_not_inside_any_single_chunk(self):
        """跨片边界的整句必然被切开 —— 因此不存在任何一个切片包含它（片内不行就查不到）。"""
        text = ("甲" * 60) + "关键字短语" + ("乙" * 60)
        chunks = chunk_text(text, ChunkConfig(strategy="auto", max_chars=60, overlap_percent=0))
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertNotIn("甲" * 50 + "关键字短语", c.body_text)

    def test_merge_short_does_not_exceed_window(self):
        text = "\n\n".join(["短"] * 40)
        cfg = ChunkConfig(strategy="custom", max_chars=150, overlap_percent=0)
        chunks = chunk_text(text, cfg)
        self.assertTrue(all(c.char_count <= 150 for c in chunks), [c.char_count for c in chunks])


class DeterminismTests(unittest.TestCase):
    def test_same_input_same_output(self):
        for cfg in (
            ChunkConfig(),
            ChunkConfig(strategy="custom", max_chars=120, overlap_percent=20),
            ChunkConfig(strategy="hierarchy", max_chars=200),
        ):
            first = chunk_text(PARA * 3, cfg)
            second = chunk_text(PARA * 3, cfg)
            self.assertEqual(
                [(c.chunk_index, c.body_text, c.start_offset, c.end_offset, c.line_start, c.line_end)
                 for c in first],
                [(c.chunk_index, c.body_text, c.start_offset, c.end_offset, c.line_start, c.line_end)
                 for c in second],
            )

    def test_records_have_stable_hashes(self):
        cfg = ChunkConfig(strategy="custom", max_chars=150, overlap_percent=0)
        r1, s1 = build_chunk_records(1, 1, PARA, cfg)
        r2, s2 = build_chunk_records(1, 1, PARA, cfg)
        self.assertEqual(s1, s2)
        self.assertEqual([r["chunk_sha256"] for r in r1], [r["chunk_sha256"] for r in r2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
