# ACCEPTANCE.md — CampusClaw 验收记录

> 本文件是**验收结论的唯一落点**。每条结论以 `openspec/` 内的规约原文为依据，不引用聊天记录（NFR-03 S2）。
>
> | 章节 | 变更 | 规约来源 |
> | --- | --- | --- |
> | §1–§5 | `add-auth-rbac-class-knowledge` | `openspec/changes/add-auth-rbac-class-knowledge/specs/auth-upload/spec.md` |
> | §6–§10 | `add-traceable-vector-retrieval` | `openspec/changes/add-traceable-vector-retrieval/specs/traceable-vector-retrieval/spec.md` |

## 0. 验收环境与总览

| 项目 | 值 |
| --- | --- |
| 运行栈 | CPython 3.13 + Flask 3.1.3 + bcrypt 5.0.0 + SQLite 3（标准库 `sqlite3`） |
| 向量库 | Qdrant（`Cosine`）；本机无 Docker，验收期使用 `scripts/qdrant_stub.py` 替身，见 §9 |
| 部署形态 | Dockerfile + docker-compose.yml（app + qdrant）+ gunicorn（NFR-02） |
| 验收日期 | 2026-09-23（变更一）／2026-09-30（变更二） |
| 数据基线 | `classes=2 / users=4 / materials=4 / knowledge_entries=4`（`python scripts/init_db.py --force`） |

复现全部结论的命令：

```bash
# 变更一
python tests/test_acceptance.py    # 场景级：23 项，覆盖 spec 每个 Scenario + A11 重启留存
python tests/http_acceptance.py    # 真实 HTTP + SQL 计数：58 项，覆盖 A1–A10 / A12
python scripts/validate_spec.py    # A13 规约校验（strict）
python scripts/check_compose.py    # T6 Docker 交付物静态校验：14 项

# 变更二
python tests/test_chunking.py                    # 35 项
python tests/test_retrieval.py                   # 68 项
python tests/test_qa.py                          # 28 项
python tests/test_search_page.py                 # 30 项
python tests/http_retrieval_acceptance.py        # 真实 HTTP：236 项 / 39 个用例
python scripts/validate_spec.py add-traceable-vector-retrieval --check-coverage
python scripts/check_compose.py
```

**总览：变更一 `23/23` + `58/58` + `0 error` + `14/14`；变更二 `161/161` 单元 + `236/236` HTTP + `0 error` + `14/14` 全部通过。**


---

## 1. 逐条 Scenario 验收（对照 spec 原文）

### FR-01 用户登录与会话建立

| # | spec 原文（Scenario） | 验证方式 | 结果 |
| --- | --- | --- | --- |
| FR-01 S1 | 教师 A 或学生 A1 在 `/login` 提交正确账号与口令 → 建立服务端会话，下发会话 Cookie；会话中记录 `user_id`、`role`、`class_id` | `test_acceptance.FR01.test_s1` + `http_acceptance A1` | ✅ 302 且 `Set-Cookie: campusclaw_session`；API 回显 `role=teacher/student`、`class_id=1` |
| FR-01 S2 | 用户名存在但口令错误 → 登录失败且不建立会话；响应不得返回密码哈希等敏感信息 | `test_acceptance.FR01.test_s2` + `http_acceptance A2` | ✅ 401，无 `Set-Cookie`，响应体不含 `$2` 与 `password_hash` |
| FR-01 S3 | 未携带有效会话访问受保护资源 → 页面 302 至 `/login`；API 401 且不含任何班级材料标题、正文或文件路径 | `test_acceptance.FR01.test_s3` + `http_acceptance A3` | ✅ `/materials`→302 `/login`；`/api/materials`→401，响应体不含「讲义」「A 班」`.md` |

### FR-02 角色权限

| # | spec 原文（Scenario） | 验证方式 | 结果 |
| --- | --- | --- | --- |
| FR-02 S1 | 学生 A1 向 `POST /api/materials/upload` 提交文件 → 返回 403；`materials` 与 `knowledge_entries` 行数不变，上传目录无新文件 | `test_acceptance.FR02.test_s1` + `http_acceptance A4` | ✅ 403（含「无权限」）；两表行数与文件数均不变 |
| FR-02 S2 | 教师 A 提交支持的文件（`.txt`/`.md`）→ 201 且响应含 `material_id`；材料与知识库条目均归属教师所属班级 | `test_acceptance.FR02.test_s2` + `http_acceptance A5` | ✅ 201；`materials.class_id = knowledge_entries.class_id = 1` |
| FR-02 S3 | 学生 A1 打开 `/materials` 或 `GET /api/materials` → 可见本班列表；提供学生可用上传/删除入口？服务端仍拒绝写接口 | `test_acceptance.FR02.test_s3` | ✅ 列表 200；页面 HTML 无 `action="/materials/upload"`；上传仍 403 |

### FR-03 班级隔离（核心）

| # | spec 原文（Scenario） | 验证方式 | 结果 |
| --- | --- | --- | --- |
| FR-03 S1 | A 班用户通过 URL 路径/路径参数/请求体指定 B 班材料 ID 访问或下载 → 403 或 404（本实现固定 404）；响应不得含 B 班标题、正文片段、文件路径或存储键 | `test_acceptance.FR03.test_s1` + `http_acceptance A7` | ✅ 详情与下载均 404；响应体不含「B 班」「一元二次方程」`.md`；不存在 ID 同为 404 |
| FR-03 S2 | 学生 A1 请求材料列表 → 每条记录均归属班级 A；列表不得出现含「B 班」的可区分标题 | `test_acceptance.FR03.test_s2` + `http_acceptance A8` | ✅ 全部 `class_id == 1`，无「B 班」标题 |
| FR-03 S3 | 教师 A 打开 `/materials` 或 `GET /api/materials` → 每条记录归属班级 A；数据来自带班级过滤的查询（`WHERE class_id = :session_class_id`） | `test_acceptance.FR03.test_s3` + `http_acceptance A8` | ✅ API 标题集合 == `SELECT title … WHERE class_id=1` 结果集；列表条数等于库内本班条数 |
| FR-03 S4 | 教师在请求体额外传入 `class_id`、`class` 等字段（如试图指定 B 班）→ 服务端忽略，材料仍写入会话所属班级；不存在「代传 B 班」的 API | `test_acceptance.FR03.test_s4` + `http_acceptance A9` | ✅ 仍 201 且 `class_id=1`；班级 B 行数未增加 |

### FR-04 材料上传与知识库入库

| # | spec 原文（Scenario） | 验证方式 | 结果 |
| --- | --- | --- | --- |
| FR-04 S1 | 教师 A 上传合法 `.md`/`.txt` 且解析成功 → `materials` +1、`knowledge_entries` 新增关联班级 A 的记录（同事务）；刷新本班列表可见该标题 | `test_acceptance.FR04.test_s1` + `http_acceptance A5/A6` | ✅ 两表各 +1，标题出现在教师与学生列表 |
| FR-04 S2 | 学生 A1 刷新 `/materials` → 可见教师 A 刚上传的材料；仍为只读，无法通过 API 修改或删除 | `test_acceptance.FR04.test_s2` + `http_acceptance A6` | ✅ 学生列表可见；上传 403；`DELETE /api/materials/<id>` 返回 404/405（无路由） |
| FR-04 S3 | 上传不支持格式（`.exe`）、空文件或解析失败（非 UTF-8 损坏文件）→ 400；两表无残留记录，且不保留孤立未关联文件 | `test_acceptance.FR04.test_s3` + `http_acceptance A12` | ✅ 三类失败均 400；两表行数与落盘文件数均不变 |

### FR-05 预置核心数据

| # | spec 原文（Scenario） | 验证方式 | 结果 |
| --- | --- | --- | --- |
| FR-05 S1 | 执行初始化脚本或 Compose 首次启动 → 存在班级 A、班级 B，以及教师 A（A 班）、学生 A1（A 班）、学生 B1（B 班）；各用户具备可登录用户名与已哈希密码 | `test_acceptance.FR05.test_s1` | ✅ 两班齐全；三名必需用户在场，`password_hash` 非明文 |
| FR-05 S2 | 查看预置材料 → 至少一条标题含「A 班」且归属班级 A，至少一条含「B 班」且归属班级 B；六类核心表均已创建 | `test_acceptance.FR05.test_s2` | ✅ 标题含「A 班」/「B 班」且归属正确；六表全部存在 |

### NFR-01 密码与密钥安全

| # | spec 原文（Scenario） | 验证方式 | 结果 |
| --- | --- | --- | --- |
| NFR-01 S1 | 查询 `users.password_hash` → 其值不等于任何预置明文口令；可识别为哈希格式（bcrypt `$2` 前缀） | `test_acceptance.NFR01.test_s1` | ✅ 全部以 `$2` 开头；`users` 表无 `password` 列 |
| NFR-01 S2 | 使用正确口令与错误口令分别登录 → 正确口令建立会话；错误口令验证失败且不建立会话 | `test_acceptance.NFR01.test_s2` | ✅ 302 vs 401 |
| NFR-01 S3 | 未提供环境变量 `SECRET_KEY` 启动应用 → 进程启动失败并给出明确原因；不落入任何不安全默认密钥 | `test_acceptance.NFR01.test_s3` | ✅ 抛 `ConfigError`；长度 < 16 同样被拒 |

### NFR-02 部署与可观测

| # | spec 原文（Scenario） | 验证方式 | 结果 |
| --- | --- | --- | --- |
| NFR-02 S1 | 按 README 执行 `docker compose up --build` 直至 healthcheck 通过 → 浏览器可访问登录页；`GET /health` 返回 200 且表明服务可用 | `scripts/check_compose.py`（静态校验）+ `test_acceptance.NFR02.test_s1` | ✅ 交付物 14 项齐全；`/health` 200 `{"status":"ok","database":"ok"}` |
| NFR-02 S2 | 不携带任何 Cookie 访问 `/health` → 仍返回成功状态码与 JSON；不发生到登录页的重定向 | `test_acceptance.NFR02.test_s2` + `http_acceptance A10` | ✅ 无 Cookie 时 200、`Content-Type: application/json`、无 `Location` 头 |
| NFR-02 S3 | `docker compose down` 后再次 `up`（未删除 volume）→ 预置班级与用户仍可登录；已上传材料与知识库记录仍可查询 | `test_acceptance.NFR02.test_s3`（等价：同一 `DATABASE`/`UPLOAD_DIR` 重建应用） | ✅ 新应用实例下材料与知识库正文仍可查 |

### NFR-03 可验证性与可追溯

| # | spec 原文（Scenario） | 验证方式 | 结果 |
| --- | --- | --- | --- |
| NFR-03 S1 | 执行验收测试 → 覆盖错误密码、未登录（页面与 API）、跨班按 ID、跨班列表、学生上传等负路径；同时覆盖登录、上传入库、列表可见等正路径 | `test_acceptance.py`（23）+ `http_acceptance.py`（58） | ✅ 负路径与正路径同等覆盖，见下方 §2 索引 |
| NFR-03 S2 | 在新会话中仅阅读 `openspec/`、README 与项目规则文件 → 可引用 spec 原文回答三条硬性要求；验收结论以 spec 原文为依据 | `test_acceptance.NFR03.test_s2` | ✅ 断言 spec 原文含三条关键句；四件套齐全 |

---

## 2. 验收编号索引 A1–A13

| 编号 | 内容 | 承载文件 | 结果 |
| --- | --- | --- | --- |
| A1 | 会话建立（教师 / 学生，含 Cookie 往返） | `http_acceptance.py` | ✅ |
| A2 | 错误密码登录失败且不泄露哈希 | `http_acceptance.py` | ✅ |
| A3 | 未登录（页面 302 / API 401）与登出后 401 | `http_acceptance.py` | ✅ |
| A4 | 学生上传 403，两表与目录无副作用 | `http_acceptance.py` | ✅ |
| A5 | 教师上传 201，同事务双表写入且同班 | `http_acceptance.py` | ✅ |
| A6 | 上传后教师与学生列表均可见 | `http_acceptance.py` | ✅ |
| A7 | 跨班按 ID 详情/下载 404，不泄露存在性 | `http_acceptance.py` | ✅ |
| A8 | 列表仅含本班，逐条校验 `class_id` | `http_acceptance.py` | ✅ |
| A9 | 上传忽略客户端 `class_id`/`class` 字段 | `http_acceptance.py` | ✅ |
| A10 | `/health` 免登录、200 JSON、不重定向 | `http_acceptance.py` | ✅ |
| A11 | 重启（down/up 未删 volume）后数据留存 | `test_acceptance.py`（等价验证） | ✅ |
| A12 | 失败路径 400 且无脏数据（三类失败） | `http_acceptance.py` | ✅ |
| A13 | 规约校验 `--strict` 无 error | `scripts/validate_spec.py` | ✅ 0 error / 0 warning |

---

## 3. 三条关键 Scenario 实测输出

### 3.1 FR-03 S1 — 跨班按 ID 访问被拒绝（核心隔离）

`http_acceptance.py` 实测（教师 A 会话请求班级 B 的材料 `id=3`）：

```
跨班访问被拒：material_id=3 属于班级 2，来访会话班级 1
127.0.0.1 - - "GET /api/materials/3 HTTP/1.1" 404 -
跨班访问被拒：material_id=3 属于班级 2，来访会话班级 1
127.0.0.1 - - "GET /api/materials/3/download HTTP/1.1" 404 -
127.0.0.1 - - "GET /api/materials/999999 HTTP/1.1" 404 -
```

`test_acceptance.py` 补充断言：响应体不含「B 班」「一元二次方程」`.md`。

### 3.2 FR-02 S1 — 学生上传被拒绝（角色权限 + 无副作用）

```
=== A4 学生上传被拒绝 ===
  [PASS] 学生上传返回 403
  [PASS] 403 消息含「无权限」
  [PASS] materials 行数不变
  [PASS] knowledge_entries 行数不变
  [PASS] 上传目录无新文件
```

### 3.3 FR-04 S1 — 上传后知识库与列表可查（同事务入库）

```
=== A5 教师上传（落盘 → 解析 → 同事务双表写入） ===
  [PASS] 教师上传返回 201
  [PASS] 响应含 material_id
  [PASS] 材料归属班级 A
  [PASS] 知识库条目归属班级 A
  [PASS] 班级 A materials +1
  [PASS] 班级 A knowledge_entries +1
=== A6 上传后列表可见（教师 + 学生） ===
  [PASS] 教师列表可见新上传材料
  [PASS] 学生列表可见新上传材料
```

---

## 4. 验证方式说明（无 Docker 环境下的等价验证）

本机未安装 Docker，Docker 相关结论以**等价方式**验证，力求结论强度不降低：

1. **交付物完整性**（NFR-02 S1 上半）：`scripts/check_compose.py` 对 Dockerfile / docker-compose.yml /
   entrypoint.sh / .env.example 做 14 项静态校验 —— 含 `build` 服务、`8080:8080` 端口映射、
   `./data` 与 `./uploads` 卷、`env_file .env`、healthcheck 指向 `/health`、entrypoint 在库缺失时执行
   `init_db.py`、以 gunicorn 绑定 `0.0.0.0:8080`、`.env.example` 不含真实密钥。**14/14 通过**。
2. **启动行为**（NFR-02 S1 下半）：`GET /health` 在真实 HTTP 下返回 200 JSON，`db.ensure_ready()`
   在库缺失时自动建表 + 写入种子 —— 与 `entrypoint.sh` 的 `init_db.py` 分支同源。
3. **数据留存**（NFR-02 S3）：以「同一 `DATABASE` + `UPLOAD_DIR` 重建应用实例」等价模拟
   `down` / `up` 不删 volume 的场景，验证材料与知识库记录仍可查询（`NFR02.test_s3`）。
4. **待补做（如有 Docker 环境）**：`docker compose up --build` 后确认 healthcheck 转为 healthy、
   浏览器可访问登录页，并执行 `down` → `up` 复核数据留存。

---

## 5. 实现取舍记录（与 spec 的一致性说明）

| 取舍点 | spec 措辞 | 本实现 | 说明 |
| --- | --- | --- | --- |
| 跨班按 ID 的状态码 | 「返回 403 或 404」 | **固定 404** | 避免以状态码差异枚举他人材料存在性；已在 README / design.md 固定 |
| 种子用户数量 | 要求教师 A、学生 A1、学生 B1（3 名） | **额外预置教师 B（4 名）** | FR-04 要求材料必须关联上传者所属班级；若 B 班样本材料署名 A 班教师，种子数据自身即破坏该不变量。教师 B 同时用于验证「B 班教师不能上传/查看 A 班」 |
| 支持的文件类型 | 未限定（MVP） | `.txt` / `.md` | 与 D5 决策一致，其余类型按 400 拒绝并保证无脏数据 |

---

---

# 变更二 `add-traceable-vector-retrieval` 验收记录

## 6. 逐条 Requirement 验收（对照 spec 原文）

规约来源：`openspec/changes/add-traceable-vector-retrieval/specs/traceable-vector-retrieval/spec.md`
（16 个 Requirement / 76 个 Scenario，逐条在下表给出承载用例）。

| # | Requirement（spec 原文标题） | Scenario 数 | 承载用例 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | 班级限定的检索范围 | 8 | `test_retrieval.VectorPrefilterTests` · `MatchingTests`；HTTP `checklist-cross-class` `checklist-tamper-ignored` `checklist-401` `checklist-isolation-predicate-required` `three-states` `search-401` | ✅ 跨班检索 200 + 空 hits；反向判据（单独移除 SQL `class_id` 谓词 / 单独移除向量 filter）用例如期失败/被回表拦住 |
| 2 | 三种检索模式 | 4 | `test_retrieval.RrfFormulaTests.test_keyword_mode_never_touches_the_vector_store`；HTTP `three-states` `checklist-qdrant-down` | ✅ `keyword` 零网关零向量调用；`vector` 真打到 `/points/search`；`hybrid` 两路均参与；`mode` 非法 400 |
| 3 | 检索输入校验与字面匹配规则 | 7 | `test_retrieval.InputValidationTests` · `MatchingTests`；HTTP `checklist-empty-query-400` | ✅ 空/超长/控制字符 400；`limit∈[1,20]`、`offset∈[0,1000]` 越界 400；ASCII 大小写折叠、`%`/`_` 按字面、`a.c` 不当正则 |
| 4 | 混合检索的融合公式与阈值 | 5 | `test_retrieval.RrfFormulaTests` · `ThresholdTests` | ✅ `1/(60+rank_kw)+1/(60+rank_vec)` 相加；单路 `1/(60+rank)`；缺席路不贡献分数；余弦 `<0.35` 丢弃、`==0.35` 保留；`k=60` / `0.35` 与课件一致 |
| 5 | 结果字段与溯源信息完整性 | 5 | `test_retrieval.HitFieldsTests` · `CoordinateSystemTests`；`test_search_page.NoVectorRenderTests`；HTTP `legacy-alias` `checklist-context-minimal` | ✅ hit 含契约 12 字段；`snippet` 取自关系库且位移可在预处理文本上复核；响应声明 `unit=unicode_code_point` / `normalization=NFC` / `offset_basis=preprocessed_text` / `snippet_object=knowledge_chunk.body_text`；向量分量不落 DOM |
| 6 | 来源读取重新鉴权与版本核对 | 4 | HTTP `source-404-shape` `source-stale-409` `source-401` | ✅ 跨班与不存在**字节级同形** 404；正文变更后 409 `source_stale` 且不返回正文；无会话 401 且无正文 |
| 7 | 切分策略与预处理 | 8 | `test_chunking.PreprocessTests` `AutoStrategyTests` `CustomStrategyTests` `HierarchyStrategyTests` `ChunkOffsetTests` `BoundaryTests` `DeterminismTests`（35 项）；HTTP `upload-then-search` `reindex-param-400` | ✅ 预处理只产出索引侧视图、不改写原文；`auto` 优先级 空行>换行>句号>硬切；`custom` 越界 400 **不静默夹取**；`hierarchy` 记章节路径；重复切分结果完全相同；emoji/组合字符/CRLF/全空白/跨片不命中均覆盖 |
| 8 | 索引生命周期与一致性 | 6 | HTTP `upload-then-search` `index-failure-rollback` `embedding-failure-keeps-material` `backfill-idempotent` `three-states` `restart-persistence`；`test_retrieval.IndexWarningTests` | ✅ 上传后立即可检索；解析失败三表无残留无孤立文件；嵌入失败保留材料 + 切片 `failed` + 不写不完整向量，正文仍可 `keyword` 命中；回填第二次不重复切片/不重复嵌入；重启后索引仍可命中；索引缺失以 `degraded` 暴露而非伪装「无命中」 |
| 9 | 显式重建索引 | 4 | HTTP `reindex-replaces` `reindex-403-student` `reindex-404-cross-class` `reindex-param-400` | ✅ 先删旧切片与旧向量再写新的，`chunk_count`/`chunk_strategy` 反映新策略；学生 403（含 `required_role`/`your_role`）且切片与向量未变；跨班 404 与材料不存在同形；参数越界 400 |
| 10 | 基于知识库的简短问答（先检索再生成） | 6 | `test_qa.NoEvidenceNeverCallsUpstreamTests` `ChatUpstreamFailureTests`；HTTP `ask-with-evidence` `ask-no-evidence-no-upstream` `ask-empty-not-404` `ask-upstream-502` `checklist-no-upstream-on-empty` | ✅ hybrid 取本班前 4 条；**0 命中时对话网关调用数 = 0**；无命中 200 + 固定文案 + `citations:[]`；网关失败 502 `upstream_error` 且无本地拼凑答案 |
| 11 | 问答上下文与 system 提示 | 4 | `test_qa.ContextAssemblyTests` `SystemDropTests` `LatestTurnOnlyTests`；HTTP `checklist-context-minimal` `checklist-system-dropped` `checklist-no-secret` `no-secret-in-response` | ✅ 上下文只含材料标题、切片序号、正文与最新一句；客户端 `system` 被丢弃，最终序列只含服务端提示；检索只用最新一句（历史不参与检索）；密钥不进响应/页面 |
| 12 | 引用标注与 citations 顺序一致 | 4 | `test_qa.CitationOrderTests`；HTTP `checklist-citation-order` `ask-with-evidence` | ✅ `answer` 中 `[n]` ↔ `citations[n-1]` 顺序一致；越界编号不产生 citation 并记 warning；citation 字段完整且可回溯到原文 |
| 13 | 认证沿用服务端会话 | 4 | HTTP `api-auth-contract` `search-401` `source-401` `checklist-401` `no-token-in-storage` | ✅ 登录态只由 `HttpOnly`+`SameSite=Lax` Cookie 承载；请求中 `class_id`/`class`/`role`/`user_id` 一律丢弃；`/api/me` 返回 `user_id`/`role`/`class_id`/`class_name`；登出后受保护接口 401；页面不使用 bearer token 与 localStorage |
| 14 | 页面安全与状态区分 | 4 | `test_search_page.EscapingTests` `SessionClearTests` `StateCopyTests` `StaleNoticeTests` `NoVectorRenderTests`（30 项）；HTTP `three-states` | ✅ 无 `innerHTML`/`eval`/行内事件，查询回显/标题/片段/正文/答案一律纯文本；401/403 清空 `hits`/`citations`/计数/回显；三态文案互不混淆且降级提示受影响材料数；409 提示重新检索且不展示过期正文 |
| 15 | 接口契约一致性 | 5 | HTTP `materials-pagination` `vectors-compare` `legacy-alias` `upload-then-search` | ✅ `/api/search`、`/api/ask` 响应形状符合契约；上传返回 `chunk_count`/`chunk_strategy`/`index_status`；`/api/materials` 追加 `page`/`page_size`/`total`/`has_more` 且不破坏既有断言；`/api/materials/<id>/vectors` 对照且不回传向量分量 |
| 16 | 范围纪律 | 5 | HTTP `checklist-qdrant-down` `checklist-cross-class`；`test_acceptance.py` + `http_acceptance.py` 回归；`scripts/validate_spec.py` | ✅ 无流式输出、无跨班检索入口、未引入新文件类型；既有 23 + 58 项验收全部保持通过；归档前仅存在于 `openspec/changes/`，未写入长期真相源 |

---

## 7. 任务 T1–T12 的 verify 实测

| 任务 | verify 命令 | 实测结果 |
| --- | --- | --- |
| T1 配置与网关适配 | `.env.example` 键检查 + `test_retrieval.GatewayConfigTests/GatewayRequestShapeTests/EmbeddingDimGuardTests` + `test_qa.ChatUpstreamFailureTests` + HTTP `no-secret-in-response` | ✅ 键齐全；请求体只含 `model`/`input`，每批 64 条；缺密钥不阻止启动、`vector`/`hybrid` 503；维度不符判失败且不写向量 |
| T2 数据层与迁移 | `knowledge_chunks`/索引存在性 + `PRAGMA table_info(materials)` 两次迁移 + 签名检查 + `test_retrieval.IndexWarningTests` | ✅ 表与两个索引就位；材料新增 5 列且迁移可重复执行；`search_chunks`/`get_chunk` 签名强制 `class_id`；`index_warnings(class_id)` 按班返回 |
| T3 切分策略 | `test_chunking` 8 个类 | ✅ 35/35 |
| T4 嵌入与向量写入 | `test_retrieval.QdrantClientTests/VectorPayloadTests` + `scripts/init_qdrant.py` 幂等 + HTTP `embedding-failure-keeps-material` | ✅ 建集合幂等（Cosine）；point id = `knowledge_chunks.id`，payload 含 `class_id` 且**不含正文**；嵌入失败保留材料、切片置 `failed`、正文仍可 `keyword` 命中 |
| T5 检索（三模式 + RRF + 阈值） | `test_retrieval` 6 个类 + `SQL_KEYWORD_SEARCH` 谓词顺序检查 + HTTP `three-states` `qdrant-down` `search-401` | ✅ 班级过滤先于匹配；`VectorPrefilterTests` 全通过；阈值/RRF 见 §6#4 |
| T6 问答链路 | `test_qa` 5 个类 + HTTP `ask-with-evidence` `ask-no-evidence-no-upstream` `ask-empty-not-404` `ask-upstream-502` `ask-auth-and-validation` | ✅ 28/28 单元 + 全部 HTTP 用例通过 |
| T7 溯源与来源端点 | HTTP `source-404-shape` `source-stale-409` `source-401` + `test_retrieval.HitFieldsTests/CoordinateSystemTests` | ✅ 同上 |
| T8 重建索引与回填 | HTTP `upload-then-search` `index-failure-rollback` `reindex-*` `backfill-idempotent` `restart-persistence` | ✅ 同上 |
| T9 接口契约补齐 | HTTP `api-auth-contract` `materials-pagination` `vectors-compare` `legacy-alias` `no-token-in-storage` | ✅ 同上 |
| T10 前端检索与问答页面 | `test_search_page` 5 个类 | ✅ 30/30 |
| T11 课件可验收清单 | HTTP `checklist-*`（12 条 + `checklist-isolation-predicate-required`） | ✅ 全部通过，见 §8 |
| T12 回归、文档与归档 | `test_acceptance.py` + `http_acceptance.py`；README / ACCEPTANCE 检查；`validate_spec.py --check-coverage` | ✅ 23/23 + 58/58 无回归；规约校验 0 error / 0 warning；归档见 §10 |

**任务勾选口径**：`tasks.md` 共 86 项，**84 项已勾选**（各自 verify 真实通过，证据见上表与本文件 §6–§8）。
**2 项保持 `[ ]`**，并且**没有**被勾选：

| 未勾选任务 | 原因 |
| --- | --- |
| Docker Compose 端到端：`docker compose up --build -d && python tests/http_retrieval_acceptance.py --base-url http://localhost:8080` | 本机无 Docker，命令无法执行。已用「交付物静态校验 14/14」+「本地替身 + 真实 HTTP 全量 236/236」等价覆盖，但不等价于容器内实跑，故不勾选 |
| 归档到 `openspec/specs/traceable-vector-retrieval/` | 规约明确「仍有未通过项时 MUST NOT 归档」。上一行未闭合，因此本项保持 `[ ]`，`.openspec.yaml` 的 `status` 也**保持 `implementing`** 而非 `verified` |

> 一处 verify 命令随实现位置做了修正：T5 的第一条原本断言 `app/search.py` 的源码里 `class_id` 先于 `instr`，
> 但本变更把检索 SQL 收进了 `app/db.py`（`SQL_KEYWORD_SEARCH`），`search.py` 只做鉴权与编排。
> 断言已改为直接检查**那条 SQL 常量本身**的谓词顺序 —— 这才是不变量，也不会随代码位置漂移。

---

## 8. 课件可验收清单（逐条）

| 课件判据 | 用例 | 结果 |
| --- | --- | --- |
| 无命中切片时不访问对话网关（"页面上出现文字 ≠ 模型被调用过"） | `checklist-no-upstream-on-empty` | ✅ 网关调用数 0 |
| 有命中时 `[1]`/`[2]` 与 `citations` 顺序一致 | `checklist-citation-order` | ✅ |
| 上下文只含材料标题、切片序号、正文与最新一句；不含向量分量、不含他班切片 | `checklist-context-minimal` | ✅ |
| 请求体自带 `system` 被丢弃，system 提示由服务端写入 | `checklist-system-dropped` | ✅ |
| 无命中返回 200 + 固定文案 + `citations: []`，不是 404/403 | `checklist-empty-200` | ✅ |
| A 班检索仅存在于 B 班正文的词 → 200 + 空 hits；同会话打开 B 班材料详情 → 404 | `checklist-cross-class` | ✅ |
| 请求体/查询串/请求头携带的 `class_id` 被丢弃，结果与不携带时一致 | `checklist-tamper-ignored` | ✅ |
| 未登录调用 `/api/search`、`/api/ask` → 401 | `checklist-401` | ✅ |
| Qdrant 停掉后 `keyword` 仍返回正文；`vector`/`hybrid` 返回 503（不编造相似度） | `checklist-qdrant-down` | ✅ |
| 空查询 → 400 | `checklist-empty-query-400` | ✅ |
| 关键路径密钥只在服务端；浏览器看不到网关密钥、无法直连 Qdrant | `checklist-no-secret` | ✅ |
| **反向判据**：单独移除 SQL `class_id` 谓词后隔离用例必须失败；单独移除 Qdrant `class_id` filter 时回表条件仍拦住 | `checklist-isolation-predicate-required` | ✅ |

---

## 9. 验证方式说明：替身验证 / 真实 Qdrant 待复核

**本机未安装 Docker，也没有可用的真实 Qdrant 与课程网关凭证。** 为不降低结论强度，
验收用两个**行为等价替身**完成，并在此逐项**如实标注差别**——下列结论属于
「替身验证通过」，**不等于**「真实 Qdrant 已复核」。

| 替身 | 文件 | 覆盖到什么程度 | 未覆盖 / 待复核 |
| --- | --- | --- | --- |
| Qdrant REST 子集替身 | `scripts/qdrant_stub.py` | 建集合（PUT 幂等、`Cosine`、size 校验）、payload 索引、upsert / delete / search（真算余弦、支持 `class_id` pre-filter 与 `score_threshold`）/ count / scroll；并**主动拒绝** payload 含正文 | Qdrant 服务端的量化、HNSW 参数、并发与持久化行为；`/points/search` 在真实服务端的分页与 `with_payload` 细节 |
| 嵌入与对话网关替身 | `tests/stubs.py`（`FakeGateway`） | 确定性 bigram 伪嵌入（维度可配）、批量切分、维度不符与条数不符的失败路径、`embed_calls`/`chat_calls` 调用计数（用于断言「零上游调用」） | 真实嵌入模型的语义相似度质量、真实对话模型的引用行为与延迟 |
| 真实 HTTP 层 | `tests/http_retrieval_acceptance.py` | 用**真 Flask + 真 HTTP + 真 SQLite**，只把上面两个外部依赖换成进程内替身 | 容器网络、反向代理、gunicorn 多 worker 下的会话与连接行为 |

**待补做（有 Docker + 真实凭证的环境）**：

1. `docker compose up --build -d`（app + qdrant），确认 healthcheck 转 healthy、`/search` 页可用；
2. `python scripts/init_qdrant.py` 对真实 Qdrant 幂等建集合；
3. `python scripts/backfill_index.py` 连真实网关回填，复核 `embedded`/`skipped` 计数与 `index_status`；
4. `python tests/http_retrieval_acceptance.py --base-url http://localhost:8080` 全量复跑；
5. 用真实嵌入模型复核阈值 0.35 是否仍有区分度（帧内余弦分布），必要时按实测调参并更新规约。

> 与设计取值无关的纪律类结论（班级隔离、引用顺序、无命中不调网关、密钥边界、回退安全）
> 不依赖真实 Qdrant，替身与真实 HTTP 下结论一致，风险低。
> 依赖服务端实现细节的结论（集合参数、索引类型、检索分页）**必须**在真实 Qdrant 上复核。

---

## 10. 实现取舍与仍未解决的限制

### 10.1 与课件的偏离声明

| 课件口径 | 本实现 | 说明 |
| --- | --- | --- |
| Go + MySQL（含 `ngram` 全文索引） | Flask 3 + SQLite，`instr(lower(body), lower(q))` | 沿用课程既有仓库技术栈。`instr` 等价于「分词长度 2 的字面包含」，且天然不做分词/同义词，与课件「字面匹配」口径一致；通配符按字面、不作为正则 |
| 向量库 Qdrant（Cosine、`class_id` 元数据过滤） | 一致 | 未偏离 |
| RRF `k=60`、阈值 `0.35` | 一致 | 未偏离 |
| 偏移单位「码点」、来源行号 | 一致 | 坐标定义写在 `coordinate_system` 响应里，可复核 |

### 10.2 仍未解决的限制

1. **真实 Qdrant 与真实网关未复核**（§9）——这是当前最主要的未闭合项。
2. **Docker Compose 端到端未实跑**：`docker-compose.yml` 已加入 `qdrant` 服务与 `qdrant_storage` 卷，
   但本机无 Docker，只做了静态校验（14/14）与「同一库路径重建应用」的等价验证。
3. **`keyword` 用 `instr` 字面匹配**，对中文长句的分词效果弱于 MySQL `ngram`：
   更长的查询词会因跨分句而命中不到。这是「字面匹配」口径的直接后果，已在 §10.1 声明。
4. **本次未做法向/性能压测**：切片量为课程规模（个位数材料），未验证万级切片下的检索延迟。
5. **归档状态**：本变更的四件套目前仍在 `openspec/changes/add-traceable-vector-retrieval/`
   （`.openspec.yaml` 的 `status: implementing`）。`tasks.md` 中「Docker Compose 端到端」一项未勾选，
   按规约「仍有未通过项时 MUST NOT 归档」，因此**不归档**到 `openspec/specs/traceable-vector-retrieval/`。
   该项闭合（或无 Docker 环境下明确由课程环境承担）后，`status` 方可改为 `verified`，随后才允许归档。

### 10.3 本轮修正的一处 verify 断言

`tasks.md` T5 的第一条 verify 原本写的是「`app/search.py` 源码里 `class_id` 出现在 `instr` 之前」。
实现过程中检索 SQL 被收进 `app/db.py` 的 `SQL_KEYWORD_SEARCH` 常量（`search.py` 只做鉴权与编排），
原断言因此失效（`ValueError: substring not found`）。已改为直接断言那条 SQL 常量本身的谓词顺序：

```
python -c "from app.db import SQL_KEYWORD_SEARCH as q;assert q.index('class_id') < q.index('instr');print('班级过滤先于匹配')"
```

这版断言更贴近不变量：它约束的是 SQL 本身，不随代码落在哪个文件而漂移。

---

*本文件所有结论均可由 §0 的命令重新生成。*

