# Tasks: add-class-scoped-knowledge-search

> **本文件只描述「变更（功能）的实现」任务。本变更当前为「规约已完成、功能未实现」状态，
> 因此下列任务全部保持 `[ ]`，不得预先勾选。**
>
> **边界说明**：第四课本轮实际完成的作业动作（编写四件套与 `.openspec.yaml`、运行严格校验、
> 更新 README、生成发布包）**不属于本变更的实现任务**，故不在此勾选；其真实结果记录在
> 仓库根的 `HOMEWORK.md`「本轮已完成的作业动作」一节。这样可保证「`[ ]` = 确实没做」这一语义为真。
>
> 每条任务 MUST 附带可执行的 `verify:`；只有 verify **真实通过**后才可勾选，并需保存脱敏证据。
> 依赖顺序：T1 → T2 → T3/T4 → T5 → T6 → T7 → T8 → T9。T3 与 T4 可在 T2 完成后并行。
> 实施前置：`add-auth-rbac-class-knowledge` 已 Apply；本变更不得修改其既有行为（回归见 T8）。
> T8 / T9 中的文档与归档项属**实现阶段**动作，须在功能落地后才可执行与勾选。

## T1 数据层与迁移

- [ ] `db.SCHEMA_SQL` 新增 `knowledge_chunks` 表与 `idx_chunks_class`、`idx_chunks_material` 索引
  - verify: `python -c "import sqlite3;c=sqlite3.connect('data/app.db');print([r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE name LIKE 'knowledge_chunks' OR name LIKE 'idx_chunks%'\")])"`
- [ ] `materials` 新增 `source_sha256`、`indexed_at`、`index_status` 三列，且迁移可重复执行
  - verify: `python scripts/init_db.py --force && python scripts/init_db.py --force && python -c "import sqlite3;c=sqlite3.connect('data/app.db');print([r[1] for r in c.execute('PRAGMA table_info(materials)')])"`
- [ ] `db.py` 新增分块查询封装，签名强制携带会话班级（`search_chunks(class_id, keyword, limit)`、`get_chunk(chunk_id, class_id)`）
  - verify: `python -c "import inspect,app.db as d;assert 'class_id' in inspect.signature(d.search_chunks).parameters and 'WHERE c.class_id = ?' in inspect.getsource(d.search_chunks);print('检索封装已强制 class_id')"`
- [ ] 迁移回退可验证：索引为派生数据，`DROP TABLE knowledge_chunks` 后既有行为不受影响
  - verify: `python -c "import sqlite3;c=sqlite3.connect('data/app.db');c.execute('DROP TABLE IF EXISTS knowledge_chunks');print('回退后 materials/knowledge_entries 仍可查询')" && python tests/http_acceptance.py`

## T2 分块与索引构建

- [ ] 实现分块算法：LF 归一化 → NFC → 空行分段 → 512 码点硬切 → 过短合并
  - verify: `python -m unittest tests.test_chunking -v`
- [ ] 每块记录 `char_start`/`char_end`（码点、左闭右开）、`line_start`/`line_end`（1 起、右闭）、`chunk_sha256`、`source_sha256`
  - verify: `python -m unittest tests.test_chunking.ChunkOffsetTests -v`
- [ ] 边界用例：emoji、组合字符、CRLF、超长无空行、全空白正文
  - verify: `python -m unittest tests.test_chunking.BoundaryTests -v`
- [ ] 分块确定性：同一正文重复分块得到完全相同的块划分与坐标
  - verify: `python -m unittest tests.test_chunking.DeterminismTests -v`

## T3 检索 API（班级过滤在候选生成之前）

- [ ] 实现 `GET /api/knowledge/search`，SQL 首条谓词为 `c.class_id = ?`，匹配/排序/计数/LIMIT 均在其后
  - verify: `python -c "import inspect,app.search as s;src=inspect.getsource(s.search);assert src.index('class_id') < src.index('instr');print('班级过滤先于匹配')"`
- [ ] 输入校验：`q` 长度 1..64 码点、拒绝控制字符；`limit` 1..20，越界返回 400 `invalid_query`
  - verify: `python -m unittest tests.test_search_input -v`
- [ ] 字面匹配：ASCII 大小写折叠、SQL 通配符转义、不作为正则求值
  - verify: `python -m unittest tests.test_search_matching -v`
- [ ] 排序与上限：`hit_count DESC` → `material_id ASC` → `ord ASC`，`LIMIT` 最后施加
  - verify: `python -m unittest tests.test_search_order -v`
- [ ] 计数只统计本班；响应不含他班计数或可推算字段
  - verify: `python -m unittest tests.test_search_count -v`
- [ ] 状态码：无会话 401、未知角色 403
  - verify: `python -m unittest tests.test_search_auth -v`

## T4 溯源 API（重新鉴权 + 版本核对）

- [ ] 实现 `GET /api/knowledge/sources/<chunk_id>`：`login_required` + `WHERE id = ? AND class_id = ?` 双条件
  - verify: `python -c "import inspect,app.search as s;assert 'class_id' in inspect.getsource(s.source);print('来源端点已双条件')"`
- [ ] 跨班与不存在返回**字节级一致**的 404
  - verify: `python -m unittest tests.test_source_404_shape -v`
- [ ] 版本核对：`chunks.source_sha256` 与当前 `materials.source_sha256` 不一致 → 409 `source_stale`，不返回正文
  - verify: `python -m unittest tests.test_source_stale -v`
- [ ] 结果字段完整性：`chunk_id`/`material_id`/`title`/`snippet`/`char_*`/`line_*`/`snippet_char_start`/`source_url`/`download_url` 全部存在
  - verify: `python -m unittest tests.test_result_fields -v`
- [ ] 链接为相对路径，不含主机名与本地文件系统路径
  - verify: `python -m unittest tests.test_result_links -v`
- [ ] 响应声明坐标系统（`unit`/`normalization`/`offset_base`/`snippet_object`）
  - verify: `python -m unittest tests.test_coordinate_system -v`

## T5 索引生命周期

- [ ] 上传路径 `_perform_upload()` 同事务写入分块并更新 `index_status`/`indexed_at`/`source_sha256`
  - verify: `python tests/http_search_acceptance.py --case upload-then-search`
- [ ] 建索引失败整体回滚：`materials`/`knowledge_entries`/`knowledge_chunks` 均无残留，无孤立文件
  - verify: `python tests/http_search_acceptance.py --case index-failure-rollback`
- [ ] `scripts/backfill_index.py` 幂等回填，按 `source_sha256` 跳过已最新材料
  - verify: `python scripts/backfill_index.py && python scripts/backfill_index.py && python tests/http_search_acceptance.py --case backfill-idempotent`
- [ ] 降级检测：索引缺失/过期 → `degraded:true` + `index_warnings`，不伪装成 `hit:false`
  - verify: `python tests/http_search_acceptance.py --case degraded-not-empty`
- [ ] 重启持久化：重启后索引仍可命中，无需全量回填
  - verify: `python tests/test_search_acceptance.py --case restart-persistence`
- [ ] 数据库故障返回 503，且响应不含任何材料内容
  - verify: `python tests/http_search_acceptance.py --case db-failure-503`

## T6 前端检索页面

- [ ] 新增 `/search` 页面，含查询输入、结果列表、来源与下载链接
  - verify: `python -c "import pathlib;assert pathlib.Path('app/templates/search.html').is_file();print('search.html 就位')"`
- [ ] 输出转义：查询回显、`title`、`snippet` 以纯文本渲染，不使用 `innerHTML` 拼接
  - verify: `python -m unittest tests.test_page_escaping -v`
- [ ] 会话失效（401/403）时清空旧结果、计数与查询回显
  - verify: `python -m unittest tests.test_page_session_clear -v`
- [ ] 三种状态文案可区分：无命中 / 索引降级 / 服务故障
  - verify: `python -m unittest tests.test_page_states -v`
- [ ] 来源过期（409）时提示重新检索，不展示过期正文
  - verify: `python -m unittest tests.test_page_stale -v`

## T7 隔离、检索与溯源测试

- [ ] 隔离用例：教师/学生各自仅见本班；无会话 401；未知角色 403
  - verify: `python tests/http_search_acceptance.py --case class-isolation`
- [ ] 跨班同关键词用例：结果与计数均不含他班
  - verify: `python tests/http_search_acceptance.py --case cross-class-same-keyword`
- [ ] 参数篡改用例：query/body/header 传入 `class_id`/`class`/`role` 均被忽略
  - verify: `python tests/http_search_acceptance.py --case tamper-ignored`
- [ ] 反向判据：移除 SQL 的 `class_id` 谓词后，隔离用例必须失败
  - verify: `python tests/http_search_acceptance.py --case isolation-predicate-required`
- [ ] 检索用例：中文、ASCII 大小写、emoji、SQL 通配符、无命中状态
  - verify: `python tests/test_search_acceptance.py --case matching`
- [ ] 溯源用例：偏移可复核、CRLF/LF 行号一致、摘要对象为块正文、哈希可核对
  - verify: `python tests/test_search_acceptance.py --case provenance`
- [ ] 边界用例：跨块边界不命中、超长硬切、空行分段、全空白材料
  - verify: `python tests/test_search_acceptance.py --case boundaries`
- [ ] 用 SQL 直查验证：被拒请求不产生任何块行、无孤立文件
  - verify: `python tests/http_search_acceptance.py --case no-dirty-rows`

## T8 回归与文档（实现阶段）

- [ ] 既有验收无回归：场景级与真实 HTTP 全套保持通过
  - verify: `python tests/test_acceptance.py && python tests/http_acceptance.py`
- [ ] 既有状态码约定不变：跨班一律 404、学生上传 403、未登录 401/302
  - verify: `python tests/http_acceptance.py`
- [ ] 实现完成后，README 的检索章节由「规划中」改为「已实现」，并补 API 用法与坐标/降级说明
  - verify: `python -c "t=open('README.md',encoding='utf-8').read();assert '/api/knowledge/search' in t and '规划中的检索能力' not in t;print('README 已转为已实现状态')"`
- [ ] 实现完成后，在 `ACCEPTANCE.md` 追加本变更的验收记录，逐条引用本 spec 原文
  - verify: `python -c "t=open('ACCEPTANCE.md',encoding='utf-8').read();assert 'class-knowledge-search' in t;print('ACCEPTANCE 已追加')"`
- [ ] 实现完成后，在 Docker Compose 启动路径下验证检索可用（有 Docker 环境时执行；无环境则留空并写明实际错误，不得用本地重启替代）
  - verify: `docker compose up --build -d && python tests/http_search_acceptance.py --base-url http://localhost:8080`

## T9 实现完成后的校验与归档

- [ ] 实现全部任务后执行严格校验，记录**真实**结果（无 CLI 环境用仓库内等价校验，并如实注明）
  - verify: `python scripts/validate_spec.py add-class-scoped-knowledge-search --check-coverage`
- [ ] 需求 / 设计 / 任务一致性检查：spec 的每个 Requirement 都能在 tasks 中找到对应任务
  - verify: `python scripts/validate_spec.py add-class-scoped-knowledge-search --check-coverage`
- [ ] 全部验收通过后，才将本变更归档到 `openspec/specs/`；仍有未通过项时 MUST NOT 归档
  - verify: `python -c "import pathlib;assert pathlib.Path('openspec/specs/class-knowledge-search').exists();print('已归档')"`（**仅在全部验收通过后**该命令才应成功）
- [ ] 报告已实现行为、测试证据、运行方式与仍未解决的限制
  - verify: `python scripts/validate_spec.py add-class-scoped-knowledge-search`
