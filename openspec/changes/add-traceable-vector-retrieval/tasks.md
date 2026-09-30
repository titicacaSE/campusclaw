# Tasks: add-traceable-vector-retrieval

> **本文件只描述「变更（功能）的实现」任务。本变更为「规约 + 实现」同轮交付：
> 任务在 `verify:` **真实通过**后才勾选，未通过的一律保持 `[ ]`。**
>
> **边界说明**：本轮实际完成的作业动作（编写/改写四件套与 `.openspec.yaml`、运行规约校验、
> 更新 README 与 HOMEWORK、生成发布包）**不属于本变更的实现任务**，故不在此勾选；
> 其真实结果记录在仓库根的 `HOMEWORK.md`。这样可保证「`[x]` = 确实已验证通过」这一语义为真。
>
> 每条任务 MUST 附带可执行的 `verify:`；只有 verify 真实通过后才可勾选，并需保存脱敏证据。
> 依赖顺序：T1 → T2 → T3 → T4 → T5 → T6/T7 → T8 → T9 → T10 → T11 → T12。T6 与 T7 可在 T5 后并行。
> 实施前置：`add-auth-rbac-class-knowledge` 已 Apply；本变更不得修改其既有行为（回归见 T12）。
> **本机无 Docker**：Qdrant 相关验证用 `scripts/qdrant_stub.py`（Qdrant 最小 REST 子集的测试替身）
> 完成，并在 `ACCEPTANCE.md` 如实标注「替身验证 / 真实 Qdrant 待复核」的差别。

## T1 配置与网关适配

- [x] 新增环境变量读取：`EMBEDDING_BASE_URL`、`EMBEDDING_API_KEY`、`EMBEDDING_MODEL`、`EMBEDDING_DIM`、`CHAT_MODEL`、`QDRANT_URL`、`QDRANT_COLLECTION`、`QDRANT_API_KEY`（可选），并写入 `.env.example`
  - verify: `python -c "import pathlib;t=pathlib.Path('.env.example').read_text(encoding='utf-8');ks=['EMBEDDING_BASE_URL','EMBEDDING_API_KEY','EMBEDDING_MODEL','EMBEDDING_DIM','CHAT_MODEL','QDRANT_URL','QDRANT_COLLECTION'];missing=[k for k in ks if k not in t];assert not missing,missing;print('.env.example 已含全部网关与向量库键')"`
- [x] 网关密钥缺失时服务 MUST 仍可启动，且读接口按降级语义响应而不是启动失败
  - verify: `python -m unittest tests.test_retrieval.GatewayConfigTests -v`
- [x] 实现 `app/gateway.py`：`embed(texts)` 调用 `POST {EMBEDDING_BASE_URL}/embeddings`，请求体只含 `model` 与 `input`
  - verify: `python -m unittest tests.test_retrieval.GatewayRequestShapeTests -v`
- [x] 校验嵌入返回维度等于 `EMBEDDING_DIM`，不等时判为失败且不写入向量
  - verify: `python -m unittest tests.test_retrieval.EmbeddingDimGuardTests -v`
- [x] 实现 `app/gateway.py` 的 `chat(messages)` 调用对话接口，超时按失败处理并返回 `502 upstream_error`，不返回本地拼凑答案
  - verify: `python -m unittest tests.test_qa.ChatUpstreamFailureTests -v`
- [x] 密钥只在服务端使用：任何响应体与页面中都不出现 `EMBEDDING_API_KEY` / `QDRANT_API_KEY` 的值
  - verify: `python tests/http_retrieval_acceptance.py --case no-secret-in-response`

## T2 数据层与迁移

- [x] `db.SCHEMA_SQL` 新增 `knowledge_chunks` 表与 `idx_chunks_class`、`idx_chunks_material` 索引
  - verify: `python -c "import sqlite3;c=sqlite3.connect('data/app.db');print([r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE name LIKE 'knowledge_chunks' OR name LIKE 'idx_chunks%'\")])"`
- [x] `materials` 新增 `source_sha256`、`indexed_at`、`index_status`、`chunk_strategy`、`chunk_config_json` 五列，且迁移可重复执行
  - verify: `python scripts/init_db.py --force && python scripts/init_db.py --force && python -c "import sqlite3;c=sqlite3.connect('data/app.db');print([r[1] for r in c.execute('PRAGMA table_info(materials)')])"`
- [x] `db.py` 新增封装，签名强制携带会话班级：`search_chunks(class_id, keyword, limit, offset)`、`count_chunks(class_id, keyword)`、`get_chunk(chunk_id, class_id)`、`list_chunks(class_id, material_id)`、`replace_chunks(material_id, class_id, chunks)`
  - verify: `python -c "import inspect,app.db as d;assert 'class_id' in inspect.signature(d.search_chunks).parameters and 'class_id' in inspect.signature(d.get_chunk).parameters;print('检索封装已强制 class_id')"`
- [x] `db.py` 新增 `index_warnings(class_id)`：返回 `index_status != 'indexed'` 的本班材料标识与状态
  - verify: `python -m unittest tests.test_retrieval.IndexWarningTests -v`
- [x] 迁移回退可验证：切片为派生数据，`DROP TABLE knowledge_chunks` 后既有行为不受影响
  - verify: `python -c "import sqlite3;c=sqlite3.connect('data/app.db');c.execute('DROP TABLE IF EXISTS knowledge_chunks');print('回退后仍可查询')" && python tests/http_acceptance.py`

## T3 切分策略

- [x] 实现 `app/chunking.py` 预处理：LF 归一化 → 可选空白折叠（保留 `\n`）→ 可选链接清洗 → NFC，且**不改写原文**
  - verify: `python -m unittest tests.test_chunking.PreprocessTests -v`
- [x] 实现 `auto` 策略：优先空行、其次换行、再次句号，最后窗口硬切；默认 `max_chars=800`、`overlap=10%`
  - verify: `python -m unittest tests.test_chunking.AutoStrategyTests -v`
- [x] 实现 `custom` 策略：`max_chars ∈ [100,2000]`、`overlap_percent ∈ [0,50]`，越界由接口层返回 400（不在切分层静默夹取）
  - verify: `python -m unittest tests.test_chunking.CustomStrategyTests -v`
- [x] 实现 `hierarchy` 策略：按 `#`/`##`/`###` 分章，单章过长再按窗口切，并记录章节路径
  - verify: `python -m unittest tests.test_chunking.HierarchyStrategyTests -v`
- [x] 每片记录 `start_offset`/`end_offset`（码点、相对预处理后文本、左闭右开）、`line_start`/`line_end`（1 起、右闭）、`chunk_sha256`、`source_sha256`
  - verify: `python -m unittest tests.test_chunking.ChunkOffsetTests -v`
- [x] 边界用例：emoji、组合字符、CRLF、超长无空行、全空白正文、跨片边界不命中
  - verify: `python -m unittest tests.test_chunking.BoundaryTests -v`
- [x] 切分确定性：同一正文同一参数重复切分得到完全相同的划分与坐标
  - verify: `python -m unittest tests.test_chunking.DeterminismTests -v`

## T4 嵌入与向量写入

- [x] 实现 `app/vector_store.py`：Qdrant REST 客户端（建集合、upsert、search 带 `class_id` filter、按 `material_id` 删除）
  - verify: `python -m unittest tests.test_retrieval.QdrantClientTests -v`
- [x] `scripts/init_qdrant.py` 幂等建集合 `campusclaw_chunks`（Cosine，size = `EMBEDDING_DIM`）与 payload 索引
  - verify: `python scripts/init_qdrant.py && python scripts/init_qdrant.py`
- [x] 向量写入以 `knowledge_chunks.id` 为 point id、以 `chunk_sha256` 为幂等键，payload 含 `class_id` 且**不含正文**
  - verify: `python -m unittest tests.test_retrieval.VectorPayloadTests -v`
- [x] 嵌入失败时材料记录保留、切片 `index_status='failed'`、**不写入不完整向量**，且正文仍可被 `keyword` 命中
  - verify: `python tests/http_retrieval_acceptance.py --case embedding-failure-keeps-material`

## T5 检索（三模式 + RRF + 阈值）

- [x] 实现 `POST /api/search`：`keyword` 模式的 SQL 首条谓词为 `c.class_id = ?`，匹配/排序/计数/LIMIT 均在其后
  - verify: `python -c "from app.db import SQL_KEYWORD_SEARCH as q;assert q.index('class_id') < q.index('instr');print('班级过滤先于匹配')"`
  - 说明：该 SQL 由 `app/db.py` 持有（`app/search.py` 只做鉴权与编排），因此断言对象是同一条 SQL 常量本身，
    而不是「哪个文件里出现了 instr」——后者会随代码位置漂移，前者才是不变量。
- [x] `vector` 模式：Qdrant 查询必须带 `class_id` **pre-filter**，回表取正文时再带一次 `class_id`（双保险）
  - verify: `python -m unittest tests.test_retrieval.VectorPrefilterTests -v`
- [x] 余弦 < 0.35 的候选被丢弃且不进入向量结果（含 `hybrid` 的向量一路）；余弦 == 0.35 保留
  - verify: `python -m unittest tests.test_retrieval.ThresholdTests -v`
- [x] `hybrid` 融合：两路 `1/(60+rank_kw) + 1/(60+rank_vec)`；单路 `1/(60+rank)`；缺席一路不贡献分数
  - verify: `python -m unittest tests.test_retrieval.RrfFormulaTests -v`
- [x] 输入校验：`q` 空/超长/含控制字符 → 400；`limit ∈ [1,20]`、`offset ∈ [0,1000]`，越界 400；`mode` 非法 400
  - verify: `python -m unittest tests.test_retrieval.InputValidationTests -v`
- [x] 排序稳定：`score_final DESC` → `material_id ASC` → `chunk_index ASC`，重复检索顺序一致
  - verify: `python -m unittest tests.test_retrieval.StableOrderTests -v`
- [x] 字面匹配：ASCII 大小写折叠、SQL 通配符按字面、不作为正则求值
  - verify: `python -m unittest tests.test_retrieval.MatchingTests -v`
- [x] 状态区分：`ok` / `no_evidence`（附 `message`）/ `degraded`（附 `index_warnings`）三者互不混淆
  - verify: `python tests/http_retrieval_acceptance.py --case three-states`
- [x] 降级：Qdrant 不可用时 `keyword` 仍返回结果，`vector`/`hybrid` 返回 503 且不编造相似度分数
  - verify: `python tests/http_retrieval_acceptance.py --case qdrant-down`
- [x] 未登录调用 `/api/search` 返回 401，响应不含任何材料内容
  - verify: `python tests/http_retrieval_acceptance.py --case search-401`

## T6 问答链路（/api/ask）

- [x] 实现 `POST /api/ask`：hybrid 检索本班前 4 条 → 有命中才调对话网关
  - verify: `python tests/http_retrieval_acceptance.py --case ask-with-evidence`
- [x] 无命中时返回 200 + 固定文案「资料中未找到相关内容」+ `citations: []`，**且不调用对话网关**
  - verify: `python tests/http_retrieval_acceptance.py --case ask-no-evidence-no-upstream`
- [x] 无命中不返回 404/403；`status` 为 `no_evidence` 且附 `message`
  - verify: `python tests/http_retrieval_acceptance.py --case ask-empty-not-404`
- [x] 上下文组装只含材料标题、切片序号、切片正文、用户最新一句；不含向量分量、Qdrant 原始点数据、他班切片
  - verify: `python -m unittest tests.test_qa.ContextAssemblyTests -v`
- [x] 客户端 `history` 中的 `system` 消息被丢弃，最终消息序列只含服务端写入的 system 提示
  - verify: `python -m unittest tests.test_qa.SystemDropTests -v`
- [x] 检索仅以最新一句 `q` 发起，`history` 不参与检索
  - verify: `python -m unittest tests.test_qa.LatestTurnOnlyTests -v`
- [x] `citations[i]` 与 `answer` 中的 `[i+1]` 顺序一致；越界引用被剔除并记 warning
  - verify: `python -m unittest tests.test_qa.CitationOrderTests -v`
- [x] 对话网关失败返回 502 `upstream_error`，响应不含本地拼凑的答案
  - verify: `python tests/http_retrieval_acceptance.py --case ask-upstream-502`
- [x] `POST /api/ask` 未登录返回 401；空查询返回 400
  - verify: `python tests/http_retrieval_acceptance.py --case ask-auth-and-validation`

## T7 溯源与来源端点

- [x] 实现来源端点：`login_required` + `WHERE id = ? AND class_id = ?` 双条件，跨班与不存在**字节级同形** 404
  - verify: `python tests/http_retrieval_acceptance.py --case source-404-shape`
- [x] 版本核对：切片 `source_sha256` 与当前 `materials.source_sha256` 不一致 → 409 `source_stale`，不返回正文
  - verify: `python tests/http_retrieval_acceptance.py --case source-stale-409`
- [x] 结果字段完整：hit 含契约全部 12 个字段；`snippet` 为切片正文子串且取自关系库
  - verify: `python -m unittest tests.test_retrieval.HitFieldsTests -v`
- [x] 响应声明坐标系统（`unit` / `offset_basis` / `normalization` / `snippet_object`），且偏移可在预处理文本上复核
  - verify: `python -m unittest tests.test_retrieval.CoordinateSystemTests -v`
- [x] 来源读取会话失效返回 401，响应不含任何正文
  - verify: `python tests/http_retrieval_acceptance.py --case source-401`

## T8 重建索引与回填

- [x] 上传路径 `_perform_upload()` 同事务写入切片；提交后嵌入并按结果置 `index_status`；响应含 `title`/`chunk_strategy`/`chunk_count`
  - verify: `python tests/http_retrieval_acceptance.py --case upload-then-search`
- [x] 解析/切分失败整体回滚：三张表均无残留、无孤立文件、返回 4xx/5xx
  - verify: `python tests/http_retrieval_acceptance.py --case index-failure-rollback`
- [x] 实现 `POST /api/materials/<id>/reindex`：先删旧切片与旧向量，再写新的；`chunk_count` 与 `chunk_strategy` 反映新策略
  - verify: `python tests/http_retrieval_acceptance.py --case reindex-replaces`
- [x] 重建参数越界返回 400（`chunk_max_chars` ∉ [100,2000]、`chunk_overlap_percent` ∉ [0,50]），不静默夹取
  - verify: `python tests/http_retrieval_acceptance.py --case reindex-param-400`
- [x] 学生会话调用重建返回 403（含 `required_role`/`your_role`），切片与向量未变化
  - verify: `python tests/http_retrieval_acceptance.py --case reindex-403-student`
- [x] 跨班重建返回 404，与材料不存在同形
  - verify: `python tests/http_retrieval_acceptance.py --case reindex-404-cross-class`
- [x] `scripts/backfill_index.py` 幂等回填：第二次不产生重复切片、不重复嵌入
  - verify: `python scripts/backfill_index.py && python scripts/backfill_index.py && python tests/http_retrieval_acceptance.py --case backfill-idempotent`
- [x] 重启持久化：重启后索引仍可命中，无需全量回填
  - verify: `python tests/http_retrieval_acceptance.py --case restart-persistence`

## T9 接口契约补齐

- [x] 新增 `POST /api/login`（Cookie 会话，失败同形）、`POST /api/logout`、`GET /api/me`（`user_id`/`role`/`class_id`/`class_name`）
  - verify: `python tests/http_retrieval_acceptance.py --case api-auth-contract`
- [x] `GET /api/materials` 支持 `page`、`page_size=20`、`title`，并在既有响应上追加 `page`/`page_size`/`total`（不破坏既有断言）
  - verify: `python tests/http_retrieval_acceptance.py --case materials-pagination`
- [x] 新增 `GET /api/materials/<id>/vectors`：对照关系库行与向量库记录，**不回传向量分量**，跨班 404
  - verify: `python tests/http_retrieval_acceptance.py --case vectors-compare`
- [x] 保留草案别名 `GET /api/knowledge/search` 与 `GET /api/knowledge/sources/<chunk_id>`，指向同一实现且保留草案期字段名
  - verify: `python tests/http_retrieval_acceptance.py --case legacy-alias`
- [x] 凭据边界：登录态只由 `HttpOnly` 会话 Cookie 承载，`localStorage` 只存主题，不使用 bearer token
  - verify: `python tests/http_retrieval_acceptance.py --case no-token-in-storage`

## T10 前端检索与问答页面

- [x] 新增 `/search` 页面：查询输入、模式选择、结果列表、来源与下载入口、问答区域
  - verify: `python -c "import pathlib;assert pathlib.Path('app/templates/search.html').is_file();print('search.html 就位')"`
- [x] 输出转义：查询回显、`material_title`、`snippet` 以纯文本渲染，不使用 `innerHTML` 拼接
  - verify: `python -m unittest tests.test_search_page.EscapingTests -v`
- [x] 会话失效（401/403）时清空 `hits`、`citations`、计数与查询回显
  - verify: `python -m unittest tests.test_search_page.SessionClearTests -v`
- [x] 三种状态文案可区分：无命中 / 索引降级 / 服务故障；降级时额外提示受影响材料数
  - verify: `python -m unittest tests.test_search_page.StateCopyTests -v`
- [x] 来源过期（409）提示重新检索，不展示过期正文
  - verify: `python -m unittest tests.test_search_page.StaleNoticeTests -v`
- [x] 页面不展示向量分量（`query_vector` 不进入 DOM）
  - verify: `python -m unittest tests.test_search_page.NoVectorRenderTests -v`

## T11 课件可验收清单（逐条）

- [x] 无命中切片时不访问对话网关（以网关调用日志证明"页面上出现文字 ≠ 模型被调用过"）
  - verify: `python tests/http_retrieval_acceptance.py --case checklist-no-upstream-on-empty`
- [x] 有命中时 `[1]`/`[2]` 与 `citations` 列表顺序一致
  - verify: `python tests/http_retrieval_acceptance.py --case checklist-citation-order`
- [x] 传给模型的上下文只含材料标题、切片序号、正文与最新一句；不含向量分量、不含他班切片
  - verify: `python tests/http_retrieval_acceptance.py --case checklist-context-minimal`
- [x] 请求体自带 `system` 消息被丢弃，system 提示由服务端写入
  - verify: `python tests/http_retrieval_acceptance.py --case checklist-system-dropped`
- [x] 无命中返回 200 + 固定文案 + `citations: []`，不是 404、不是 403
  - verify: `python tests/http_retrieval_acceptance.py --case checklist-empty-200`
- [x] A 班检索仅存在于 B 班正文的词 → 200 + 空 hits；同一会话打开 B 班材料详情 → 404
  - verify: `python tests/http_retrieval_acceptance.py --case checklist-cross-class`
- [x] 请求体/查询串/请求头携带的 `class_id` 被丢弃，结果与不携带时完全一致
  - verify: `python tests/http_retrieval_acceptance.py --case checklist-tamper-ignored`
- [x] 未登录调用 `/api/search`、`/api/ask` → 401
  - verify: `python tests/http_retrieval_acceptance.py --case checklist-401`
- [x] Qdrant 停掉后 `keyword` 仍返回正文；`vector`/`hybrid` 返回 503（不编造相似度）
  - verify: `python tests/http_retrieval_acceptance.py --case checklist-qdrant-down`
- [x] 空查询 → 400
  - verify: `python tests/http_retrieval_acceptance.py --case checklist-empty-query-400`
- [x] 关键路径密钥只在服务端；浏览器网络面板看不到网关密钥，也无法直连 Qdrant
  - verify: `python tests/http_retrieval_acceptance.py --case checklist-no-secret`
- [x] 反向判据：单独移除 SQL 的 `class_id` 谓词后隔离用例必须失败；单独移除 Qdrant 的 `class_id` filter 时回表条件仍拦住
  - verify: `python tests/http_retrieval_acceptance.py --case checklist-isolation-predicate-required`

## T12 回归、文档与归档

- [x] 既有验收无回归：场景级与真实 HTTP 全套保持通过
  - verify: `python tests/test_acceptance.py && python tests/http_acceptance.py`
- [x] 既有状态码约定不变：跨班材料详情一律 404、学生上传 403、未登录 401/302
  - verify: `python tests/http_acceptance.py`
- [x] README 更新：检索与问答能力、接口表、坐标与降级说明、与课件的偏离声明
  - verify: `python -c "t=open('README.md',encoding='utf-8').read();assert '/api/ask' in t and '/api/search' in t and 'Qdrant' in t;print('README 已更新')"`
- [x] `ACCEPTANCE.md` 追加本变更验收记录，逐条引用本 spec 原文，并标注「替身验证 / 真实 Qdrant 待复核」
  - verify: `python -c "t=open('ACCEPTANCE.md',encoding='utf-8').read();assert 'traceable-vector-retrieval' in t;print('ACCEPTANCE 已追加')"`
- [ ] Docker Compose 启动路径下检索与问答可用（有 Docker 环境时执行；无环境则写明实际错误，不得用本地重启替代）
  - verify: `docker compose up --build -d && python tests/http_retrieval_acceptance.py --base-url http://localhost:8080`
- [x] 实现全部任务后执行严格校验，记录**真实**结果（无 CLI 环境用仓库内等价校验，并如实注明）
  - verify: `python scripts/validate_spec.py add-traceable-vector-retrieval --check-coverage`
- [x] 需求 / 设计 / 任务一致性检查：spec 的每个 Requirement 都能在 tasks 中找到对应任务
  - verify: `python scripts/validate_spec.py add-traceable-vector-retrieval --check-coverage`
- [ ] 全部验收通过后才归档到 `openspec/specs/traceable-vector-retrieval/`；仍有未通过项时 MUST NOT 归档
  - verify: `python -c "import pathlib;assert pathlib.Path('openspec/specs/traceable-vector-retrieval').exists();print('已归档')"`（**仅在全部验收通过后**该命令才应成功）
- [x] 报告已实现行为、测试证据、运行方式与仍未解决的限制
  - verify: `python scripts/validate_spec.py add-traceable-vector-retrieval`
