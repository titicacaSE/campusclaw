"""检索与问答页面（`/search`）的渲染纪律测试（T10）

覆盖 tasks.md 的类名：EscapingTests / SessionClearTests / StateCopyTests / StaleNoticeTests /
NoVectorRenderTests。

为什么是「结构断言」而不是「跑一遍 JS」
----------------------------------------
本机没有 Node 前端测试链，也不该为一个单页引入构建步骤。但这些纪律（不外拼标记、
会话失效即清空、409 不展示过期正文、向量分量不进 DOM）恰好都是**静态可判**的：
它们约束的是「代码里有没有这条路径」，而不是「这条路径跑出来什么」。
因此这里把模板源码切开做结构断言，并额外要求「没有别的写法能绕过」——
例如只要页面里出现 `innerHTML` 就判失败，无论它在哪条分支上。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEMPLATE = ROOT / "app" / "templates" / "search.html"

# 允许出现「把文本写进节点」的三种写法；除此之外都不允许
TEXT_SINKS = ("textContent", "createTextNode")

# 会把字符串当标记解析的写法 —— 页面上出现任何一个都判失败
MARKUP_SINKS = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                "eval(", "new Function")


def page_source() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def script_source() -> str:
    """只取 <script> 里的内容，避免把 HTML 注释里的说明当成实现。"""
    text = page_source()
    match = re.search(r"<script>(.*?)</script>", text, re.S)
    assert match, "search.html 里找不到 <script> 段"
    return match.group(1)


def strip_comments_js(source: str) -> str:
    """去掉 JS 注释：注释里提到 `innerHTML` 是为了说明纪律，不该被当成违规。"""
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    return re.sub(r"//[^\n]*", " ", source)


def executable_script() -> str:
    return strip_comments_js(script_source())


def function_body(name: str) -> str:
    """粗略取一个函数的函数体（按大括号配对）。"""
    src = executable_script()
    start = src.index("function " + name)
    brace = src.index("{", start)
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[brace: idx + 1]
    raise AssertionError(f"函数 {name} 的大括号不配对")


class EscapingTests(unittest.TestCase):
    def setUp(self):
        self.raw = page_source()
        self.js = executable_script()

    def test_template_exists_and_is_served_for_the_search_route(self):
        self.assertTrue(TEMPLATE.is_file())
        self.assertIn("search.html", (ROOT / "app" / "search.py").read_text(encoding="utf-8"))

    def test_no_markup_sink_anywhere_on_the_page(self):
        for sink in MARKUP_SINKS:
            self.assertNotIn(sink, self.js, f"页面上出现了 {sink}")

    def test_only_text_sinks_are_used(self):
        self.assertIn("textContent", self.js)
        self.assertIn("createTextNode", self.js)

    def test_echoed_query_title_and_snippet_are_rendered_as_text(self):
        body = function_body("renderHits")
        self.assertIn("hit.material_title", body)
        self.assertIn("hit.snippet", body)
        # 两处都必须走 textContent，而不是拼接标记
        self.assertGreaterEqual(body.count("textContent"), 3)

    def test_source_body_is_rendered_as_text(self):
        body = function_body("loadSource")
        self.assertIn("res.body.body_text", body)
        self.assertIn("textContent", body)
        self.assertNotIn("innerHTML", body)

    def test_answer_text_is_rendered_as_text(self):
        body = function_body("renderAnswer")
        self.assertIn("res.answer", body)
        self.assertIn("textContent", body)

    def test_alert_text_is_rendered_as_text(self):
        # alertBox 把文案交给 makeEl（内部走 textContent），而不是自己拼标记
        body = function_body("alertBox")
        self.assertIn("makeEl('div', 'alert', text)", body)
        self.assertNotIn("innerHTML", body)
        self.assertIn("textContent", function_body("makeEl"))

    def test_no_inline_event_handler_attributes(self):
        for attr in ("onclick=", "onerror=", "onload="):
            self.assertNotIn(attr, self.raw, attr)

    def test_server_side_template_does_not_mark_user_content_as_safe(self):
        self.assertNotIn("|safe", self.raw)
        self.assertNotIn("Markup(", self.raw)

    def test_helper_writes_text_through_textContent_only(self):
        body = function_body("makeEl")
        self.assertIn("node.textContent", body)
        self.assertNotIn("innerHTML", body)


class SessionClearTests(unittest.TestCase):
    def setUp(self):
        self.js = executable_script()

    def test_clear_all_resets_state_nodes_and_echoed_inputs(self):
        body = function_body("clearAll")
        for field in ("state.hits", "state.citations", "state.query", "state.warnings"):
            self.assertIn(field, body, field)
        for node in ("'hits'", "'citations'", "'answer-slot'", "'source-slot'"):
            self.assertIn(node, body, node)
        self.assertIn("$('q').value = ''", body.replace('"', "'"))
        self.assertIn("$('ask-q').value = ''", body.replace('"', "'"))

    def test_search_401_403_clears_everything(self):
        body = function_body("handleSearchError")
        self.assertIn("status === 401", body)
        self.assertIn("status === 403", body)
        self.assertIn("clearAll()", body)

    def test_ask_401_403_clears_everything(self):
        body = function_body("handleAskError")
        self.assertIn("status === 401", body)
        self.assertIn("status === 403", body)
        self.assertIn("clearAll()", body)

    def test_source_401_403_clears_everything(self):
        body = function_body("loadSource")
        self.assertIn("res.status === 401", body)
        self.assertIn("res.status === 403", body)
        self.assertIn("clearAll()", body)

    def test_hits_are_not_assigned_before_the_response_is_verified(self):
        """401/403 时不得把结果先塞进 state —— 否则清空等于白做。"""
        src = self.js
        submit = src.index("$('search-form').addEventListener('submit'")
        ask = src.index("$('ask-form').addEventListener('submit'")
        search_handler = src[submit:ask]
        self.assertLess(search_handler.index("if (!res.ok)"),
                        search_handler.index("state.hits = "))

    def test_three_entry_points_all_have_a_clear_path(self):
        self.assertGreaterEqual(self.js.count("clearAll()"), 3)


class StateCopyTests(unittest.TestCase):
    def setUp(self):
        self.js = executable_script()

    def test_three_states_have_distinguishable_copy(self):
        state_fn = function_body("renderSearchState")
        self.assertIn("no_evidence", state_fn)
        self.assertIn("degraded", state_fn)          # 降级
        self.assertIn("无命中（未调用大模型）", state_fn)

        error_fn = function_body("handleSearchError")
        self.assertIn("503", error_fn)               # 服务故障
        self.assertIn("关键词", error_fn)             # 给出可用的降级路径

    def test_no_evidence_copy_comes_from_the_server_message(self):
        state_fn = function_body("renderSearchState")
        self.assertIn("res.message", state_fn)

    def test_degraded_copy_reports_the_number_of_affected_materials(self):
        state_fn = function_body("renderSearchState")
        self.assertIn("res.index_warnings", state_fn)
        self.assertIn(".length", state_fn)
        self.assertIn("份材料的索引未就绪", state_fn)

    def test_service_fault_is_not_confused_with_no_evidence(self):
        state_fn = function_body("renderSearchState")
        error_fn = function_body("handleSearchError")
        self.assertNotIn("503", state_fn)            # 服务故障不走「状态」位
        self.assertNotIn("no_evidence", error_fn)

    def test_ask_error_copy_states_no_locally_patched_answer(self):
        body = function_body("handleAskError")
        self.assertIn("502", body)
        self.assertIn("不会", body)


class StaleNoticeTests(unittest.TestCase):
    def setUp(self):
        self.js = executable_script()

    def test_409_invites_a_fresh_search(self):
        body = function_body("loadSource")
        self.assertIn("409", body)
        self.assertIn("重新检索", body)

    def test_409_branch_never_renders_expired_body_text(self):
        body = function_body("loadSource")
        start = body.index("if (res.status === 409)")
        end = body.index("if (res.status === 404)", start)
        stale_branch = body[start:end]
        self.assertIn("return;", stale_branch)         # 立刻返回，不继续往下渲染
        self.assertNotIn("body_text", stale_branch)
        self.assertNotIn("textContent = res.body", stale_branch)

    def test_404_branch_reports_absence_without_leaking_anything(self):
        body = function_body("loadSource")
        self.assertIn("404", body)
        self.assertIn("来源不存在", body)

    def test_source_endpoint_is_the_only_place_that_fetches_bodies(self):
        self.assertEqual(self.js.count("'/api/knowledge/sources/'"), 1)


class NoVectorRenderTests(unittest.TestCase):
    def setUp(self):
        self.js = executable_script()

    def test_query_vector_never_reaches_the_dom(self):
        self.assertNotIn("query_vector", self.js)

    def test_no_vector_components_are_rendered(self):
        for banned in ("embedding", "vector_data", "point_payload", "chunk_sha256"):
            self.assertNotIn(banned, self.js, banned)

    def test_ranking_meta_shows_ranks_not_vector_values(self):
        body = function_body("renderHits")
        self.assertIn("score_final", body)
        self.assertIn("rank_keyword", body)
        self.assertIn("rank_vector", body)
        self.assertNotIn("score_vector", body)        # 相似度原始分数不下发页面

    def test_page_declares_modes_without_exposing_backend_urls(self):
        raw = page_source()
        self.assertIn("/api/search", raw)
        self.assertIn("/api/ask", raw)
        for banned in ("QDRANT_URL", "EMBEDDING_API_KEY", "qdrant", "api_key"):
            self.assertNotIn(banned, raw, banned)

    def test_page_does_not_read_credentials_from_storage(self):
        for banned in ("localStorage", "sessionStorage", "Authorization", "Bearer"):
            self.assertNotIn(banned, self.js, banned)


if __name__ == "__main__":
    unittest.main(verbosity=2)
