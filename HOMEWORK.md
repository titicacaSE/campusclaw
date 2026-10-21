# 第四课作业说明：可溯源向量检索与基于本班资料的问答

> 第五、六课（AI 应用平台：智能体 / 技能 / MCP 工具）的作业说明见
> [`HOMEWORK-AGENT-PLATFORM.md`](HOMEWORK-AGENT-PLATFORM.md)；两者是同一仓库的连续变更。

> 仓库：CampusClaw（学期工程，Flask 3.x + SQLite 3 + Qdrant）
> 本次变更：`add-traceable-vector-retrieval`　新增能力：`traceable-vector-retrieval`
> 变更目录：`openspec/changes/add-traceable-vector-retrieval/`
> 变更状态：**规约与实现同轮交付**；`tasks.md` 86 项中 **84 项已通过各自的 verify 并勾选**，
> 2 项（Docker Compose 端到端、归档到 `openspec/specs/`）因本机无 Docker 与归档前置条件未闭合而**保持 `[ ]`**
> 取代关系：早期草案 `add-class-scoped-knowledge-search`（把问答列入 Non-goals，与课件口径冲突）已被本变更取代

## 一、作业要求与本次交付

| 项 | 作业要求 | 本次交付 |
| --- | --- | --- |
| 认证方式 | 会话 Cookie（不用 token） | ✅ 登录态只由 `HttpOnly` + `SameSite=Lax` Cookie 承载；`POST /api/login` / `/api/logout` / `GET /api/me`；`localStorage` 只存主题 |
| 检索范围 | **限定班级**，`class_id` 取自会话 | ✅ `class_id` 是 SQL 的**第一条谓词**，也是 Qdrant 的 **pre-filter**；请求体/查询串/请求头里的班级字段一律丢弃 |
| 检索模式 | 关键词 / 向量 / 混合**三种** | ✅ `keyword`（只查关系库，零网关零向量）/ `vector`（嵌入 + Qdrant 余弦）/ `hybrid`（RRF 融合） |
| 向量库 | Qdrant | ✅ `Cosine`，payload 索引用于 `class_id` 过滤；`scripts/init_qdrant.py` 幂等建集合 |
| 融合算法 | RRF，`k = 60` | ✅ `1/(60+rank_kw) + 1/(60+rank_vec)`；单路 `1/(60+rank)`；缺席路不贡献分数 |
| 相似度阈值 | 余弦 **< 0.35 丢弃** | ✅ 等号保留（`== 0.35` 命中），阈值在服务端与本地各判一次 |
| 问答纪律 | **先检索再生成**；无命中**不调模型** | ✅ 0 命中时对话网关调用数为 **0**，返回固定文案「资料中未找到相关内容」+ `citations: []` + 200 |
| 引用 | `[n]` 与 citations 顺序一致 | ✅ `answer` 中 `[n]` ↔ `citations[n-1]`；越界编号不产生 citation 并记 warning |
| 内容溯源 | 片段 + 材料/片段 ID + 码点偏移 + 行号 + 来源与下载链接 | ✅ 并显式声明坐标系（`unicode_code_point` / `NFC` / `preprocessed_text` / `knowledge_chunk.body_text`） |
| 切分策略 | 支持多种切分并可重建 | ✅ `auto` / `custom` / `hierarchy`；`POST /api/materials/<id>/reindex`；`scripts/backfill_index.py` 幂等回填 |
| 降级语义 | 依赖不可用时不得伪装 | ✅ `vector`/`hybrid` 503、来源过期 409、索引未就绪 `degraded` + `index_warnings` |
| 严格校验 | `openspec validate --strict` | ⚠️ 本机无 `openspec` CLI，用仓库内等价校验并**如实记录**（见第三节） |
| 可公开仓库 | 提交公开仓库地址 | ✅ `https://github.com/titicacaSE/campusclaw`（public） |

## 二、变更内容概要

**为什么做**：上一变更让材料能上传入库、也能按关键词定位到原文片段，但材料只被当成一坨文本——
`knowledge_entries` 每篇材料仅一行、`ord` 恒为 `0`，没有切片，也就没有向量，无法做语义召回；
而课件要求的是「可溯源的向量检索 + 有据可查的问答」。

**本次新增什么**

1. `app/chunking.py`：预处理（LF 归一化 → 可选空白折叠 / 链接清洗 → NFC，**不改写原文**）
   + 三种切分策略（`auto` / `custom` / `hierarchy`），每片记录码点偏移、行号、块哈希与来源哈希。
2. `knowledge_chunks` 表 + `materials` 五列（`source_sha256` / `indexed_at` / `index_status` /
   `chunk_strategy` / `chunk_config_json`），迁移幂等、回退安全（切片是派生数据）。
3. `app/gateway.py`：嵌入与对话网关适配（**密钥只在服务端**），嵌入请求体只含 `model` 与 `input`。
4. `app/vector_store.py` + `scripts/init_qdrant.py`：Qdrant 客户端（建集合、upsert、带 `class_id`
   pre-filter 的 search、按材料删除）；point id = `knowledge_chunks.id`，payload **不含正文**。
5. `app/indexing.py`：两阶段索引编排——「切分入库」与调用方同事务，「嵌入写向量」在提交后进行；
   向量失败不毁掉正文（材料保留、切片置 `failed`、`keyword` 仍可命中）。
6. `app/retrieval.py`：三模式检索 + RRF 融合 + 阈值 + 输入校验 + 结果模型与坐标系声明。
7. `app/qa.py`：先检索再生成（hybrid 取本班前 4 条）；**服务端组装 `system` 与上下文**，
   客户端 `system` 一律丢弃；解析引用并剔除越界编号。
8. `POST /api/materials/<id>/reindex`、`GET /api/materials/<id>/vectors`、
   `GET /api/materials` 分页、`POST /api/login` / `/api/logout` / `GET /api/me`。
9. `/search` 页面：三模式检索 + 提问 + 引用 + 来源；纯 `textContent` 渲染、三态文案、会话失效即清空。
10. `scripts/backfill_index.py` 幂等回填；`scripts/qdrant_stub.py`（Qdrant 最小 REST 子集替身，
    用于无 Docker 环境的行为验证）。

**不做什么**（Non-goals）：流式输出、对话历史持久化、多轮会话管理、跨班检索入口、新文件类型（PDF/OCR）、
重排序模型、材料级 ACL、管理员后台。

## 三、严格校验结果（真实）

| 命令 | 真实结果 |
| --- | --- |
| `python scripts/validate_spec.py add-traceable-vector-retrieval` | **0 error / 0 warning** |
| `python scripts/validate_spec.py add-traceable-vector-retrieval --check-coverage` | **0 error / 0 warning**（含需求→任务覆盖检查） |
| `openspec validate add-traceable-vector-retrieval --strict` | **未能执行：本机无 `openspec` CLI**（全局与 `npx` 均不可用） |

> **未执行就是未执行。** 上表第三行在本机跑不起来，因此用仓库内 `scripts/validate_spec.py`
> 作等价校验。**有 `openspec` CLI 的环境应补跑该命令复核**，不得以本地等价校验替代官方 CLI 的结论。

**校验器非空转的反向测试**（证明它能抓到问题，而非永远通过）：

| 人为制造的违规 | 是否被抓到 |
| --- | --- |
| 原始状态（已通过 verify 的任务勾选） | 0 error（正确） |
| 任务勾选但缺少 verify 命令 | ✅ 抓到（违反 `require_verify_per_task`） |
| 删除 `## ADDED Requirements` | ✅ 抓到 |
| 删除 proposal 五节 | ✅ 抓到 |
| 把任务勾选状态改回未实现却仍标 `verified` | ✅ 抓到 |

**本次测试的真实结果**：

| 命令 | 结果 |
| --- | --- |
| `python tests/test_chunking.py` | **35/35 OK** |
| `python tests/test_retrieval.py` | **68/68 OK** |
| `python tests/test_qa.py` | **28/28 OK** |
| `python tests/test_search_page.py` | **30/30 OK** |
| `python tests/http_retrieval_acceptance.py` | **236/236 通过（39 个用例）** |
| `python tests/test_acceptance.py`（回归） | 23/23 **OK** |
| `python tests/http_acceptance.py`（回归） | 58/58 **通过** |
| `python scripts/check_compose.py` | 14/14 **通过** |

**已知局限**：覆盖检查只验证关键关切项**是否在 tasks.md 中被提及**，用于防止整块能力被遗漏；
它**不能**判断实现正确性。行为正确性由 spec 的 Scenario 与 `http_retrieval_acceptance.py` 负责。

## 四、关键设计决策（摘要）

| # | 决策 | 选择 | 一句话理由 |
| --- | --- | --- | --- |
| D1 | 检索实现 | SQLite `instr(lower(body), lower(q))` | 沿用课程既有栈；等价于「分词长度 2 的字面包含」，结果完全确定 |
| D2 | 班级过滤位置 | SQL 首条谓词 **+** Qdrant pre-filter **+** 回表再带一次 | 三道闸；反向判据可证明少任何一道都会泄露 |
| D3 | 索引写入 | 「切分/入库」同事务，「嵌入/写向量」在提交后 | 切分失败整体回滚；向量失败不毁掉正文 |
| D4 | 偏移坐标 | Unicode 码点，相对**预处理后**文本，左闭右开 | 与 Python `str` 一致；NFC 后长度稳定，偏移可复核 |
| D5 | 融合 | RRF `k=60`，两路各自 `1/(60+rank)` 相加 | 与课件一致；不引入需要调权的分数归一化 |
| D6 | 阈值 | 余弦 `<0.35` 丢弃，`==0.35` 保留 | 与课件「小于则丢弃」的字面口径一致 |
| D7 | 无命中 | **不调用对话网关**，固定文案 + 空 citations | 「页面上有文字」不等于「模型被调用过」，不能让模型凭记忆硬答 |
| D8 | system 提示 | 只由服务端写入 | 否则客户端一句「忽略以上规则」即可绕过 |
| D9 | `index_status` | `pending → indexing → indexed / failed` | `failed` 表示向量缺失而非材料丢失，语义可区分 |
| D10 | 来源过期 | `409 source_stale`，不返回正文 | 内嵌正文会让过期片段看起来仍有效 |

**最关键的实现约束**：班级过滤 MUST 出现在两条路径的**候选生成之前**，
并要求用「单独移除 SQL 谓词 → 隔离用例必须失败」与「单独移除 Qdrant filter → 回表仍拦住」
作为**反向判据**（`checklist-isolation-predicate-required`）。

## 五、本轮已完成的作业动作（含证据）

| 动作 | 产物 | 证据 |
| --- | --- | --- |
| 读课件新要求，改写为可验收规约 | `proposal.md` / `design.md` / `specs/traceable-vector-retrieval/spec.md` / `tasks.md` | 16 个 Requirement、76 个 Scenario、86 项任务 |
| 声明取代旧草案 | `.openspec.yaml` | `supersedes: add-class-scoped-knowledge-search` |
| 实现切分与坐标 | `app/chunking.py` | `test_chunking.py` 35/35 |
| 实现网关适配 | `app/gateway.py` | 请求体只含 `model`/`input`；维度不符判失败 |
| 实现向量库客户端 | `app/vector_store.py`、`scripts/init_qdrant.py` | payload 不含正文；建集合幂等 |
| 实现索引编排 | `app/indexing.py`、`app/knowledge.py` | 三表同事务；向量失败置 `failed` |
| 实现检索 | `app/retrieval.py`、`app/search.py` | `test_retrieval.py` 68/68；HTTP 236/236 |
| 实现问答 | `app/qa.py`、`app/search.py` | `test_qa.py` 28/28；无命中网关调用数 0 |
| 实现页面 | `app/templates/search.html` | `test_search_page.py` 30/30 |
| 实现脚本 | `scripts/backfill_index.py`、`scripts/qdrant_stub.py` | 回填第二次不重复切片/不重复嵌入 |
| 补接口契约 | `app/api_auth.py`、`app/materials.py` | `api-auth-contract` / `materials-pagination` / `vectors-compare` |
| 更新文档 | `README.md`、`ACCEPTANCE.md`、`.env.example`、`docker-compose.yml`、`entrypoint.sh` | README 含接口表与偏离声明；ACCEPTANCE 逐条引用 spec 原文 |
| 回归既有验收 | — | 23/23 + 58/58 无回归 |

## 六、可复用提示词

以下提示词可直接粘贴到同类项目的 AI 编程助手中，用于「把课件要求落成可验收的规约 + 实现」，
且**不会把"写规约"误当成"功能已实现"**。

```text
请在当前仓库以 OpenSpec 变更的形式交付一份课件要求，并严守「规约完成 ≠ 功能已实现」，
以及「未通过 verify 的任务不得勾选」。

第一步：先读 AGENTS.md、openspec/config.yaml、openspec/changes/ 下已有变更的四件套与现有代码，
明确区分【已实现能力】与【本次拟新增能力】。若课件资料缺失，明确说明资料限制，不编造需求。

第二步：在 openspec/changes/<change-name>/ 新建四件套 + .openspec.yaml：
1. proposal.md：Why / What Changes / Non-goals / Capabilities / Impact，附【前置依赖】与【交付状态】。
2. design.md：数据结构、算法与公式（写清常数取值）、API、生命周期、坐标与溯源、安全边界、
   降级语义、取舍、迁移与回退。
3. specs/<capability>/spec.md：## ADDED Requirements，用 MUST/SHALL 与 WHEN/THEN/AND 场景写可验收行为。
4. tasks.md：按依赖拆成可执行任务，每项写 verify；尚未实施的任务全部保持 [ ]。
5. .openspec.yaml：声明 depends_on、supersedes（若有）、allow_prechecked_tasks、
   require_verify_per_task、require_evidence_per_checked_task。

第三步：实施。按 tasks 依赖顺序推进，需求变化先改本变更 Markdown 再改代码。
每完成一项立刻执行其 verify，真实通过才勾选，并保存脱敏证据。

设计硬性要求：
- 角色与数据边界一律来自服务端会话；客户端 query/body/header 不得参与授权。
- 边界过滤必须发生在【候选生成、匹配、排序、计数、top-k/limit 之前】，
  且对向量库使用等价的前置过滤；写出反向判据：单独移除任一道过滤，隔离用例必须失败或被下一道拦住。
- 密钥只在服务端：请求体、响应体、页面、localStorage 都不得出现；嵌入与对话请求体只提交必要字段。
- 检索必须区分并写清：无权限、参数非法、不存在、状态过期、服务故障、无结果——各自的状态码与响应形态。
- 外部依赖不可用时的降级语义必须写死，不得伪造分数或结果；派生数据失败不得毁掉主数据。
- 生成类能力必须「先检索再生成」：检索为空时【不得调用生成模型】，返回固定文案与空引用。
  提示词与上下文由服务端组装，客户端传入的 system 角色一律丢弃。
- 引用与出处必须可机器核对（编号 ↔ 列表下标；越界引用剔除并记 warning）；
  上下文只允许含检索到的素材与用户最新一句。
- 派生数据（切片/索引）必须可重建、可幂等回填；缺失或过期不得伪装成"没有结果"。
- 明确 Non-goals 并在 spec 中写一条"范围纪律"需求，防止实现越界。

第四步：验收与交付。
- 分层验收：函数层单元测试（算法/公式/阈值/隔离/转义）+ 真实 HTTP 验收（状态码/契约/回归）。
- 无 Docker、无真实外部服务时，用行为等价的替身完成验收，并在验收文档中【逐条标注
  "替身验证 / 真实服务待复核"的差别】，写明未覆盖的部分。
- 运行严格校验（openspec validate <change-name> --strict；无 CLI 时用仓库内等价脚本并如实记录），
  并做一次反向测试证明校验器不是空转。
- 跑既有全部回归；更新 README、ACCEPTANCE.md 与本次作业说明。
- 全部验收通过后才归档到 openspec/specs/；仍有未通过项时 MUST NOT 归档。
- 最后报告：变更目录、主要设计、真实校验结果、可复用提示词、仓库链接，
  以及仍未解决的限制。
```

## 七、发布包与仓库

### 仓库

| 项 | 值 |
| --- | --- |
| 公开仓库 | `https://github.com/titicacaSE/campusclaw` |
| 可见性 | public |
| 推送方式 | SSH（本机 HTTPS 到 github.com 会被重置） |
| 发布目录 | `campusclaw-semester/`（本校规工程） |

### 发布包

生成于 `dist/`，内容仅含**可公开的源码、规约与说明**。

**排除项（MUST）**：`.env`（真实密钥）、`data/*.db`（含哈希口令的数据库）、`uploads/**`（上传原件）、
`__pycache__/`、`.venv/`、临时运行日志、任何有效 Cookie 或会话凭证、
**网关与向量库密钥（`EMBEDDING_API_KEY` / `CHAT_API_KEY` / `QDRANT_API_KEY`）**。

**发布前须知（需人工确认的一项）**：`app/seed.py` 与 `README.md` 含**课程演示账号口令**
（如 `teacher_a` / `TeacherA@2026`）。保留原因：

- 它们是**演示数据**，批改者需要用它们登录验证；README 必须写明；
- `tests/` 直接 `from app.seed import DEMO_PASSWORDS`，移除会同时打断种子数据与全部测试。

它们是**课程演示凭据，不是真实凭据**。若公开仓库不允许出现任何明文口令，
可改为「种子口令从环境变量读取，本地用 `.env` 提供」——但这会改变已验收的行为，
需按规约流程先改本变更 Markdown 再改代码。

## 八、完成度声明

| 问题 | 回答 |
| --- | --- |
| 变更描述是否完成？ | **是**。四件套 + `.openspec.yaml` 就位，严格校验 0 error / 0 warning。 |
| 功能是否已实现？ | **是**。`tasks.md` 86 项中 84 项已通过各自的 verify 并勾选。 |
| 是否还有未勾选的任务？ | **是，2 项**：`docker compose up --build -d` 端到端（本机无 Docker，跑不起来）、归档到 `openspec/specs/`（规约要求全部验收通过后才可归档，本轮尚有前者未闭合）。**未执行就是未执行，不勾选。** |
| 测试是否真实通过？ | **是**。单元 161/161 + HTTP 236/236 + 既有回归 23/23 与 58/58。 |
| 是否已归档到 `openspec/specs/`？ | **否**。§9 的「真实 Qdrant 待复核」尚未闭合，`.openspec.yaml` 仍为 `status: implementing`。 |
| 上一变更的完成状态是否被改动？ | **未改动**，其 23 + 58 项验收仍全部通过。 |
| 公开仓库是否已发布？ | **是**，见第七节。 |

> **「验收通过」也有边界。** 本机无 Docker、无真实 Qdrant 与课程网关凭证，
> 向量与对话两侧由行为等价替身验证（详见 `ACCEPTANCE.md` §9）。
> 纪律类结论（班级隔离、引用顺序、无命中不调网关、密钥边界、回退安全）不依赖具体服务端实现，
> 风险低；而集合参数、索引类型、检索分页等依赖服务端细节的结论**必须在真实 Qdrant 上复核**。
