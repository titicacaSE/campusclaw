# Proposal: add-traceable-vector-retrieval

> 来源：第 4 课课件《登录认证与知识库问答》要求摘录（2026-09-30 已发布，共 7 页）+
> `/week04/` 教学原型实测接口契约 + 原第四课作业要求（限定班级范围的知识库检索、支持内容溯源、向量库可选）。
> 前置变更：`add-auth-rbac-class-knowledge`（已完成 Apply，见其 `tasks.md`）。
> **本变更取代**早期草案 `add-class-scoped-knowledge-search`（该草案在课件未发布时起草，
> 把问答列入 Non-goals，与课件口径冲突，已废弃）。
> 新增能力：`traceable-vector-retrieval`　变更名：`add-traceable-vector-retrieval`
> 位置：`openspec/changes/add-traceable-vector-retrieval/`

## Why

上一变更建立了身份、角色与班级隔离，教师可上传材料、学生可只读浏览本班内容。但知识库目前**只能把材料整篇打开来看**：

- `knowledge_entries` 每篇材料仅一行、`ord` 恒为 `0`、正文不分块，**没有任何偏移坐标**，无法回答「这句话在哪份讲义的哪一段」；
- 没有检索入口，只能靠人翻；上一变更的 Non-goals 明确排除了「RAG、全文检索」；
- 引用无法回溯到原文位置，也无法判断引用是否已过期或被改过。

**课件已给出本课的正式要求，且比早期草案更宽**：第 4 课不仅要**检索**，还要做**基于本班知识库的简短问答**（带 `[1]` 出处标注），并明确「先检索，再生成」——「当本班材料中不存在依据时，不将问题提交给生成模型」。只有**流式长对话**留给第 5 课。

本变更的目标：在**不突破班级边界**的前提下，交付「**分块 → 嵌入 → 入库 → 检索（关键字 / 向量 / 混合）→ 生成（有切片才生成简短回答）**」五步链路，并让每一处引用都能**回溯到原文坐标**。

## What Changes

1. **分块索引**：新增 `knowledge_chunks` 表，把材料正文切分为可定位的块，记录块正文、相对全文的**码点偏移**、行号范围、块哈希与来源哈希；支持课件规定的三种切分策略（`auto` / `custom` / `hierarchy`）。
2. **向量嵌入与向量库**：接入课程网关 `/v1/embeddings` 生成定长向量，写入 **Qdrant** 集合 `campusclaw_chunks`；Qdrant 只存向量与 payload 标识，**正文始终留在关系库**。
3. **三种检索模式**：`keyword`（只查关系库全文/字面匹配，不调嵌入、不访问 Qdrant）、`vector`（余弦相似度，**低于 0.35 的候选丢弃**）、`hybrid`（两路各自过滤后按 **RRF，k = 60** 融合，缺席的一路不贡献分数）。
4. **溯源 API**：读取来源时**重新鉴权**并**核对正文版本**；不一致返回 `409 source_stale`，绝不返回与当前正文不符的片段。
5. **问答 API** `POST /api/ask`：先由检索模块以混合模式取回**本班前 4 条**切片；**无切片则直接返回固定文案且不调用对话网关**；有切片时连同编号交给模型，回答以 `[1]` 指回出处，`citations` 顺序与编号一致。
6. **索引生命周期**：上传时同事务建切片并写入向量；历史材料提供**幂等回填**；提供**显式重建**接口（换切分策略必须先删旧切片与旧向量）；`index_status` 状态机（`pending` / `indexed` / `stale` / `failed`）。
7. **降级语义**：**Qdrant 不可用时，`keyword` 仍可返回结果；`vector` 与 `hybrid` 返回 `503`，且不编造相似度分数**。
8. **接口契约对齐课件**：新增 `POST /api/login`、`POST /api/logout`、`GET /api/me`、`POST /api/search`、`POST /api/ask`、`POST /api/materials/:id/reindex`、`GET /api/materials/:id/vectors`，并把 `GET /api/materials` 补为分页；**第 3 课既有端点一律保留**（见 Capabilities）。
9. **前端页面**：新增 `/search` 检索与问答页；转义查询词、标题与片段；凭据全走同源 Cookie，`localStorage` **只存主题**。

## Non-goals

| 类别 | 不做的内容 |
| --- | --- |
| 对话形态 | **流式输出（SSE / chunked）与多轮长对话**（课件明确留第 5 课）；聊天式会话历史持久化 |
| 检索范围 | 跨班检索、全校检索、跨材料聚合统计、全库问答 |
| 其他业务域 | 作业提交与批改、成绩、错题本、管理员后台、审计报表 |
| 文件类型 | PDF、OCR、音视频转写（沿用上一变更：仅 `.txt` / `.md`） |
| 检索算法 | 分词器、同义词、模糊匹配、拼音纠错、rerank 重排模型、多向量/混合向量模型 |
| 向量库职责 | **不把切片正文存进 Qdrant**（正文只在关系库）；不用向量库替代关系库做权威数据源 |
| 权限模型 | 材料级 ACL、公开材料、分享链接、匿名访问 |
| 前端替代服务端安全 | 不以隐藏按钮、前端过滤作为班级隔离的手段；**不把网关密钥下发到浏览器** |
| 模型自由度 | 不允许模型自行决定用它记忆的知识作答；`system` 提示由服务端写入，客户端传入的 `system` 消息一律丢弃 |

> **注意**：「生成式问答」**不在**本 Non-goals 内 —— 它是本课的正式交付内容。只有**流式**形态被排除。

## Capabilities

- `traceable-vector-retrieval`：**可溯源的向量检索 + 基于本班知识库的简短问答**（**本次新增**）。
- 依赖的既有能力：`auth-upload`（登录、会话、角色、班级隔离、材料上传入库）——**已完成**，本变更复用其 `session["class_id"]` / `session["role"]` 与 `materials` / `knowledge_entries` 结构。

**既有端点的处置（回归硬约束）**

| 类别 | 端点 | 处置 |
| --- | --- | --- |
| 第 3 课既有 | `GET /login`（表单）、`POST /login`、`/logout`、`GET /api/materials`、`GET /api/materials/<id>`、`GET /api/materials/<id>/download`、`POST /api/materials/upload`、`POST /materials/upload`、`/materials` 页面、`/health` | **保留，行为不变**（既有 23 + 58 项验收 MUST 全绿） |
| 本课新增（课件契约） | `POST /api/login`、`POST /api/logout`、`GET /api/me`、`POST /api/search`、`POST /api/ask`、`POST /api/materials/<id>/reindex`、`GET /api/materials/<id>/vectors` | **新增** |
| 早期草案端点 | `GET /api/knowledge/search`、`GET /api/knowledge/sources/<chunk_id>` | **保留为兼容别名**（同一实现，指向新的检索/来源服务），避免草案期已写下的验收判据失效 |

## 前置依赖 (Prerequisites)

| 依赖 | 当前状态 | 说明 |
| --- | --- | --- |
| 登录与会话（`session["user_id"|"role"|"class_id"]`） | **已实现** | `app/auth.py` 的 `login_required` / `teacher_required` |
| 班级隔离约定（会话取 `class_id`） | **已实现** | `db.get_material(id, class_id)` 双条件；跨班统一 404 |
| 材料与正文存储（同事务） | **已实现** | `materials` + `knowledge_entries` |
| 下载链接可复用 | **已实现** | `GET /api/materials/<id>/download`（本班成员可下载） |
| **分块与偏移坐标** | **不存在** | 本变更新增 |
| **检索入口与索引表** | **不存在** | 本变更新增 |
| **课程网关接入凭据** | **已具备** | 学号 + 姓名登录网关后取得 `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` / `EMBEDDING_DIM`（**仅从环境变量注入，不下发浏览器**） |
| **Qdrant 向量库** | **待接入** | 集合 `campusclaw_chunks`，余弦距离；本机无 Docker，见 design.md D3 与本表下方说明 |
| 演示用材料正文（供检索验收） | **已实现** | 种子含两班可区分材料，可作检索语料 |

> 前置依赖未全部满足时 MUST NOT 实施本变更。已实现 6 项、待本变更新增 2 项、待环境接入 1 项（Qdrant）。

## 与课件参考实现的偏离（显式声明）

课件参考实现为 **Go + MySQL 8 + Qdrant**（含 `MySQL FULLTEXT ... WITH PARSER ngram`、token 长度 2）。本组沿用第 3 课已验收的 **Flask 3 + SQLite 3** 技术栈，仅向量库与课件一致。偏离项、理由与等价性论证 MUST 逐条记录在 `design.md` §11，并在 `README.md` 的偏离声明中同步。**偏离不影响任何对外可见行为**：接口路径、请求/响应字段、状态码、融合公式、阈值、班级隔离语义均按课件契约实现。

## Impact

- **数据层**：新增表 `knowledge_chunks`；`materials` 新增 `source_sha256`、`indexed_at`、`index_status`、`chunk_strategy`、`chunk_config_json`。
- **API**：新增 9 个端点（见 Capabilities 表）；既有端点行为不变。
- **页面**：新增 `/search`。既有 `/login`、`/materials` 不变（仅补一个指向 `/search` 的入口）。
- **脚本**：新增 `scripts/backfill_index.py`（幂等回填）、`scripts/init_qdrant.py`（建集合与 payload 索引）。
- **上传事务**：`materials._perform_upload()` 需在同事务内额外写入切片；**向量写入在事务提交后执行**，失败时把 `index_status` 置 `failed` 但保留材料记录（课件：「嵌入失败时材料记录仍保留，对应切片标记为 failed，不写入不完整向量」）。
- **配置**：新增 `EMBEDDING_BASE_URL`、`EMBEDDING_API_KEY`、`EMBEDDING_MODEL`、`EMBEDDING_DIM`、`CHAT_MODEL`、`QDRANT_URL`、`QDRANT_COLLECTION`、`QDRANT_API_KEY`（可选）。**密钥缺失时服务 MUST 仍可启动**，读接口按降级语义响应。
- **测试**：新增检索模式、融合公式、阈值、切分策略、重建索引、问答无命中不调模型、system 丢弃、引用顺序、降级 503、密钥不下发等用例；既有 23 + 58 项回归 MUST 保持全绿。
- **不改动**：登录与会话机制、角色权限、上传权限（仅教师）、下载权限、Docker 启动方式、`.txt` / `.md` 类型白名单、`/health`。

## 交付状态 (Delivery Status)

**本变更为「规约 + 实现」同轮交付**：先按本目录四件套定稿规约并通过校验，再按 `tasks.md` 实施代码与验收。

| 交付物 | 状态 |
| --- | --- |
| `proposal.md` / `design.md` / `specs/traceable-vector-retrieval/spec.md` / `tasks.md` | **已完成**（本目录） |
| `.openspec.yaml` | **已完成** |
| 代码实现（表、切分、网关适配、Qdrant 客户端、检索、问答、页面、脚本、测试） | 见 `tasks.md` 勾选状态（**未勾选即未完成**） |
| `openspec/specs/` 归档 | **未归档** —— 只有在本变更**全部验收通过**后，方可由归档流程写入 |
| 上一变更 `add-auth-rbac-class-knowledge` 的完成状态 | **保持原样，不回溯勾选任何未做的验收项** |
| 已废弃草案 `add-class-scoped-knowledge-search` | **已由本变更取代并移除**（其有效内容已并入本目录） |

## 需求变更纪律

本变更的需求变化 MUST 先改本目录的 Markdown，再改代码；`openspec/specs/` 仅在全部验收通过后通过归档更新（AGENTS.md 第一条与第四条）。
