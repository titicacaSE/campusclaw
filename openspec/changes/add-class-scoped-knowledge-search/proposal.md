# Proposal: add-class-scoped-knowledge-search

> 来源：第四课作业要求（实现**限定班级范围**的知识库检索，支持**内容溯源**；向量数据库可选）
> 前置变更：`add-auth-rbac-class-knowledge`（已完成 Apply，见其 `tasks.md`）
> 新增能力：`class-knowledge-search`　变更名：`add-class-scoped-knowledge-search`
> 位置：`openspec/changes/add-class-scoped-knowledge-search/`

## Why

上一变更建立了身份、角色与班级隔离，教师可上传材料、学生可只读浏览本班内容。但知识库目前**只能把材料整篇打开来看**：

- `knowledge_entries` 每篇材料仅一行，`ord` 恒为 `0`，正文不分块，**没有任何偏移或行号坐标**；
- 因此无法回答「这句话在哪份讲义的哪一段」；
- 任何引用都无法回溯到原文位置，也无法判断引用是否已过期或被改过；
- 上一变更的 Non-goals 明确排除了「RAG、全文检索」，检索能力**尚不存在**。

本变更的目标：在**不突破班级边界**的前提下提供关键词检索，并为每条结果提供**可回溯到原文的溯源信息**（材料、片段、码点偏移、行号、版本哈希）。

## What Changes

1. **分块索引**：新增 `knowledge_chunks` 表，把材料正文切分为可定位的块，记录块正文、相对全文的码点偏移、行号范围、块哈希与生成时的来源哈希。
2. **班级限定检索 API**：`GET /api/knowledge/search`，检索范围在 SQL 的**第一条谓词**上即限定为会话所属班级。
3. **溯源 API**：`GET /api/knowledge/sources/<chunk_id>`，读取来源时**重新鉴权**并**核对正文版本**。
4. **索引生命周期**：上传时同事务建索引；历史材料提供**幂等回填**脚本；索引缺失或过期时响应**显式降级**。
5. **失败与边界**：明确输入长度、字面匹配规则、排序、结果上限、无命中状态，以及 401 / 403 / 404 的统一行为。
6. **前端页面**：`/search` 检索页，安全转义查询词、标题与片段。

## Non-goals

| 类别 | 不做的内容 |
| --- | --- |
| 对话与生成 | 聊天界面、RAG 问答、LLM 生成答案或摘要、Agent 编排、技能包运行时 |
| 检索范围 | 跨班检索、全校检索、跨材料聚合统计 |
| 其他业务域 | 作业提交与批改、成绩、错题本、管理员后台、审计报表 |
| 文件类型 | PDF、OCR、音视频转写（沿用上一变更：仅 `.txt` / `.md`） |
| 检索算法 | 分词、同义词、模糊匹配、拼音纠错、**语义向量召回**（本变更不采用，理由见 `design.md` D6 与 §8） |
| 权限模型 | 材料级 ACL、公开材料、分享链接、匿名访问 |
| 前端替代服务端安全 | 不以隐藏按钮、前端过滤作为班级隔离的手段 |

本变更**不包含答案生成**：只返回可回溯的原文片段与定位信息，不生成任何新文本，因此不存在「虚构引用」的空间（见 `design.md` §6）。

## Capabilities

- `class-knowledge-search`：班级限定知识库检索 + 内容溯源（**本次新增**）。
- 依赖的既有能力：`auth-upload`（登录、会话、角色、班级隔离、材料上传入库）——**已完成**，本变更复用其 `session["class_id"]` / `session["role"]` 与 `materials` / `knowledge_entries` 结构，不修改其行为。

## 前置依赖 (Prerequisites)

| 依赖 | 当前状态 | 说明 |
| --- | --- | --- |
| 登录与会话（`session["user_id"|"role"|"class_id"]`） | **已实现** | `app/auth.py` 的 `login_required` / `teacher_required` |
| 班级隔离约定（`WHERE class_id = ?`） | **已实现** | `db.get_material(id, class_id)` 双条件；跨班统一 404 |
| 材料与正文存储（同事务） | **已实现** | `materials` + `knowledge_entries` |
| 下载链接可复用 | **已实现** | `GET /api/materials/<id>/download`（本班成员可下载） |
| **分块与偏移坐标** | **不存在** | 上一变更每篇材料仅一行、`ord` 恒 `0`、无偏移 → 本变更新增 |
| **检索与溯源入口** | **不存在** | 上一变更 Non-goals 明确排除 → 本变更首次引入 |
| 演示用材料正文（供检索验收） | **已实现** | 种子含两班可区分材料，可作检索语料 |

> 前置依赖未全部满足时 MUST NOT 实施本变更。当前已实现项 5 项、待本变更新增项 2 项。

## Impact

- **数据层**：新增表 `knowledge_chunks`；`materials` 预留 `source_sha256`、`indexed_at`、`index_status` 三列（本变更实施时写入）。
- **API**：新增 `GET /api/knowledge/search`、`GET /api/knowledge/sources/<chunk_id>`。
- **页面**：新增 `/search`。既有 `/login`、`/materials` 不变。
- **脚本**：新增 `scripts/backfill_index.py`（幂等回填历史材料索引）。
- **上传事务**：`materials._perform_upload()` 需在同事务内额外写入分块并更新索引状态（沿用「失败无脏数据」契约）。
- **测试**：新增检索隔离、溯源、回填、降级、分块边界、参数篡改等用例；既有 23 + 58 项回归 MUST 保持全绿。
- **不改动**：登录、会话、角色、上传权限、下载权限、Docker 启动方式、`.txt`/`.md` 类型白名单。

## 交付状态 (Delivery Status)

**本变更当前处于「规约已完成、功能未实现」状态。**

| 交付物 | 状态 |
| --- | --- |
| `proposal.md` / `design.md` / `specs/class-knowledge-search/spec.md` / `tasks.md` | **已完成**（本目录） |
| `.openspec.yaml` | **已完成** |
| 代码实现（表、API、页面、脚本、测试） | **未开始**，`tasks.md` 全部任务保持 `[ ]` |
| `openspec/specs/` 归档 | **未归档** —— 只有在本变更**全部验收通过**后，方可由归档流程写入 |
| 上一变更 `add-auth-rbac-class-knowledge` 的完成状态 | **保持原样，不回溯勾选任何未做的验收项**（其 Docker 相关项维持既有真实状态） |

> **「规约完成」≠「功能已实现」。** 本目录只描述将要交付的行为与判定标准；仓库中**尚无**对应实现代码，`tasks.md` 中没有任何一项被勾选。

## 需求变更纪律

本变更的需求变化 MUST 先改本目录的 Markdown，再改代码；`openspec/specs/` 仅在全部验收通过后通过归档更新（AGENTS.md 第一条与第四条）。
