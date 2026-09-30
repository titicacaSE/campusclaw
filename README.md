# CampusClaw

面向中小学的教研智能体平台。仓库当前包含两个 OpenSpec 变更：

| 变更 | 能力 | 状态 |
| --- | --- | --- |
| `add-auth-rbac-class-knowledge` | 登录、角色权限、班级隔离、材料上传与知识库入库 | **已实现并通过验收** |
| `add-traceable-vector-retrieval` | 可溯源向量检索（三模式 + RRF）、基于本班资料的问答与引用 | **已实现并通过验收** |

> 早期草案 `add-class-scoped-knowledge-search` 把「问答生成」列入 Non-goals，与课件口径冲突，
> 已由 `add-traceable-vector-retrieval` **取代**（见其 `.openspec.yaml` 的 `supersedes`）。

## 价值

把教研材料从「群文件 + 网盘」搬进有归属、有权限的班级知识库，让教师上传一次、全班可查；
检索结果能回溯到原文的具体位置，问答也能逐条给出处，而不是「凭模型记忆」作答。

## 场景

教师登录后把讲义（`.txt` / `.md`）上传到本班知识库，服务端解析文本、切分切片、写入向量库；
学生登录后只读本班材料列表。教师或学生输入关键词，只在本班材料中检索，每条结果给出
**原文片段、材料与片段标识、码点偏移、源行号、来源链接与下载链接**，可回溯到原文。
也可以直接提问：服务端先在本班检索前 4 条切片，再让模型**只依据这几条**作答，并给出 `[n]` 引用。

跨班访问一律被服务端拒绝：跨班检索对外是 **200 + 空结果**（不用 403/404 暗示他班资料存在），
跨班材料详情与跨班来源读取一律 **404**。

## 不做

不做作业流程 / 多校多租户 / SSO / K8s / PDF-OCR；不以隐藏按钮代替服务端权限校验。

**问答的边界**：模型只允许依据本次检索到的本班切片作答，不得凭自身记忆、常识或猜测补充事实；
资料不足时固定回答「资料中未找到相关内容」。没有命中切片时**根本不调用对话网关**，
所以不会出现「页面上有文字，但其实模型没被调用过」这种假象，也不会出现「模型硬答」。

---

## 快速开始

### 方式一：Docker Compose（标准启动方式，规约 NFR-02）

```bash
cp .env.example .env                                  # 填写 SECRET_KEY（必填，缺失启动即失败）
python -c "import secrets;print(secrets.token_urlsafe(48))"   # 生成一个随机密钥填进 .env
# 可选：填 EMBEDDING_* / CHAT_* 启用向量检索与问答；不填也能起，只是 vector/hybrid 返回 503
docker compose up --build                             # 启动 app + qdrant；首次自动建表 + 写入种子数据
curl -sf http://127.0.0.1:8080/health && echo           # {"status":"ok",...}
```

打开 <http://127.0.0.1:8080/login> 登录。`docker compose down` 后再 `up`（未删 volume）数据仍在。

### 方式二：本地直接运行

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export SECRET_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(48))")   # Windows PowerShell: $env:SECRET_KEY='...'
python scripts/init_db.py                          # 建表 + 种子（幂等）

# 向量库（三选一，不配也能起，只是 vector/hybrid 不可用）
docker run -p 6333:6333 qdrant/qdrant:v1.12.4      # 真 Qdrant
# 或： python scripts/qdrant_stub.py --port 6333    # 测试替身（无 Docker 时用，行为等价性见文件头）
export QDRANT_URL=http://127.0.0.1:6333
export EMBEDDING_BASE_URL=... EMBEDDING_API_KEY=... EMBEDDING_MODEL=... EMBEDDING_DIM=1024
export CHAT_MODEL=...                              # CHAT_BASE_URL/CHAT_API_KEY 留空则复用嵌入侧

python scripts/init_qdrant.py                      # 幂等建集合
python scripts/backfill_index.py                   # 幂等回填：为已有材料补切片与向量
python wsgi.py                                     # http://127.0.0.1:8080
```

### 演示账号（仅课程演示环境，生产必须替换）

| 账号 | 角色 | 所属班级 | 口令 |
| --- | --- | --- | --- |
| `teacher_a` | 教师 | 班级 A | `TeacherA@2026` |
| `teacher_b` | 教师 | 班级 B | `TeacherB@2026` |
| `student_a1` | 学生 | 班级 A | `StudentA1@2026` |
| `student_b1` | 学生 | 班级 B | `StudentB1@2026` |

> 规约只要求教师 A、学生 A1、学生 B1 三名用户；本实现额外预置了「教师 B（班级 B）」。
> 原因是 FR-04 要求材料必须关联「上传者所属班级」——若 B 班样本材料署名为 A 班教师，种子数据自身就会破坏该不变量；
> 同时教师 B 可用于验证「B 班教师既不能上传到 A 班，也不能查看 A 班材料」。

---

## 状态码与隔离约定（固定取舍）

- **未登录**：页面请求 `302 → /login`；`/api/**` 请求 `401 {"error":"unauthorized"}`。
- **学生调用写接口**：`403 {"error":"forbidden"}`（消息含「无权限」）。
- **按 ID 访问跨班或不存在材料**：**统一返回 `404`**（规约 FR-03 S1 允许 403 或 404，本实现择一固定为 404）。
  理由是避免通过状态码差异枚举他人材料是否存在；响应体不含 B 班标题、正文、文件路径或存储键。
  来源端点 `GET /api/knowledge/sources/<chunk_id>` 同样如此（跨班与不存在**字节级同形**）。
- **跨班检索**：`200` + 空 `hits`（不用 403/404 暗示资料属于其他班级）。
- **上传失败**（类型不支持 / 空文件 / 超限 / 解析失败）：`400`，且不产生任何 DB 行与孤立文件。
- **来源过期**（材料正文已变）：`409 {"error":"source_stale"}`，**不返回正文**。
- **向量库 / 嵌入网关不可用**：`503`（`vector_store_unavailable` / `embedding_unavailable`），
  **不编造相似度分数**；`keyword` 模式仍照常返回正文。
- **对话网关失败**：`502 {"error":"upstream_error"}`，**不返回本地拼凑的答案**。
- `class_id` 一律取自服务端会话，请求体 / 查询串 / 请求头中的 `class_id` / `class` / `role` / `user_id`
  一律**丢弃**；不存在「代传其他班」的 API。

## 接口

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/health` | 公开 | 200 JSON；数据库不可读时 503；不重定向 |
| GET | `/login` · POST `/login` | 公开 | 表单登录；失败返回 401 且不建立会话 |
| POST/GET | `/logout` | 登录 | 清空会话 |
| POST | `/api/login` | 公开 | JSON 登录（Cookie 会话，失败同形 401，不回显口令相关差异） |
| POST | `/api/logout` | 登录 | 清空会话并下发删除 Cookie |
| GET | `/api/me` | 登录 | `user_id` / `role` / `class_id` / `class_name`；未登录 401 |
| GET | `/materials` | 登录 | 本班材料列表页（教师额外显示上传表单） |
| GET | `/search` | 登录 | **检索与问答页面**（三模式检索 + 提问 + 引用 + 来源） |
| GET | `/api/materials` | 登录 | 本班材料列表（JSON）；支持 `page` / `page_size=20` / `title`，返回 `total` / `has_more` |
| GET | `/api/materials/<id>` | 登录 | 材料详情 + 知识库条目；跨班 404 |
| GET | `/api/materials/<id>/download` | 登录 | 下载原文件；跨班 404 |
| GET | `/api/materials/<id>/vectors` | 登录 | 关系库切片 ↔ 向量库记录对照（`consistent`）；**不回传向量分量**；跨班 404 |
| POST | `/api/materials/upload` | **教师** | multipart 上传 → 解析 → 同事务入库切片 → 建向量 → 201 |
| POST | `/api/materials/<id>/reindex` | **教师** | 换切分策略重建：先删旧切片与旧向量，再写新的；学生 403、跨班 404、参数越界 400 |
| POST | `/materials/upload` | **教师** | 页面表单入口，成功后 302 回列表页 |
| POST | `/api/search` | 登录 | **三模式检索**：`keyword` / `vector` / `hybrid`；`q`、`limit`(1–20)、`offset`(0–1000)、`mode` |
| POST | `/api/ask` | 登录 | **基于本班资料问答**：hybrid 取前 4 条 → 有命中才调对话网关 → 返回 `answer` + `citations[]` |
| GET | `/api/knowledge/search?q=<词>&limit=<1..20>` | 登录 | 草案别名（保留旧字段名），指向 `keyword` 模式同一实现 |
| GET | `/api/knowledge/sources/<chunk_id>` | 登录 | 来源读取：**重新鉴权 + 双条件 + 版本核对**；跨班/不存在同形 404，过期 409 |

### 检索与问答的行为约定

- **班级过滤位置**：`keyword` 的 SQL 里 `c.class_id = ?` 是**第一条谓词**，先于匹配、计数、排序与 `LIMIT`；
  `vector` 的 Qdrant 查询带 `class_id` **pre-filter**，回表取正文时**再带一次** `class_id`（双保险）。
  禁止全库检索后再隐藏。
- **三种模式**：`keyword` 只查关系库（不调嵌入、不访问 Qdrant）；`vector` 走嵌入 + Qdrant 余弦；
  `hybrid` 两路各取候选后做 **RRF 融合，k = 60**：`1/(60+rank_kw) + 1/(60+rank_vec)`，
  只有单路命中时就退化为 `1/(60+rank)`，**缺席的一路不贡献分数**（不是分数相加）。
- **相似度阈值**：余弦 **< 0.35 丢弃**，`== 0.35` 保留（含 `hybrid` 的向量一路）。
- **切分策略**：`auto`（默认空行 > 换行 > 句号，`max_chars=800`、重叠 10%）、
  `custom`（`max_chars ∈ [100,2000]`、`overlap_percent ∈ [0,50]`，越界返回 400 **不静默夹取**）、
  `hierarchy`（按 `#`/`##`/`###` 分章）。预处理为「LF 归一化 → 可选空白折叠/链接清洗 → NFC」，**不改写原文**。
- **坐标系统**：偏移单位是 **Unicode 码点**，相对 **预处理后文本**，左闭右开；行号为 1 起、右闭；
  摘要对象是 `knowledge_chunk.body_text`。响应里显式声明这套坐标（`coordinate_system`）。
- **降级**：索引未就绪时返回 `degraded:true` + `index_warnings`（含受影响材料数与状态），
  **不得**返回空结果冒充「无命中」；`no_evidence` 与 `degraded` 与 `ok` 三态互不混淆。
- **引用纪律**：`answer` 中的 `[n]` 与 `citations[n-1]` 顺序一致；越界编号不产生 citation 并记 warning。
- **密钥纪律**：网关与向量库密钥**只在服务端**；任何响应体、页面、`localStorage` 都不出现，
  页面也不直连 Qdrant、不使用 bearer token（会话只由 `HttpOnly` + `SameSite=Lax` Cookie 承载）。

### 与课件的偏离声明

课件示例用 **Go + MySQL**；本实现沿用课程既有仓库的 **Flask 3 + SQLite** 技术栈，因此：

| 课件口径 | 本实现 | 说明 |
| --- | --- | --- |
| MySQL `ngram` 全文索引 | SQLite `instr(lower(body), lower(q))` | 等价于「分词长度 2 的字面包含」；通配符按字面、不作为正则 |
| —— | 向量库 Qdrant | 与课件一致（`Cosine`，`class_id` payload 索引用于 pre-filter） |
| —— | RRF `k=60`、阈值 `0.35` | 与课件一致 |

> 「源行号」与「码点偏移」的坐标定义、以及「无命中不调用对话网关」这条纪律，均与课件一致，未作放宽。

## 目录结构

```
app/
  __init__.py          # create_app：SECRET_KEY 强制校验；网关/向量库缺失不阻止启动
  db.py                # SQLite 封装（强制携带 class_id 的检索/切片查询）
  auth.py api_auth.py  # 会话登录、角色校验、/api 契约
  materials.py         # 材料 CRUD、上传（同事务切分）、reindex、vectors 对照
  knowledge.py         # materials + knowledge_entries + knowledge_chunks 同事务写入
  chunking.py          # 预处理 + auto/custom/hierarchy 切分（坐标与行号）
  gateway.py           # 嵌入 / 对话网关适配（只从环境变量读密钥）
  vector_store.py      # Qdrant REST 客户端（建集合、upsert、class_id pre-filter、按材料删）
  indexing.py          # 「切分入库 → 嵌入 → 写向量 → 置 index_status」两阶段编排
  retrieval.py         # 三模式检索 + RRF + 阈值 + 输入校验 + 结果模型
  qa.py                # 先检索再生成、上下文组装、引用解析
  search.py            # /api/search、/api/ask、来源端点、草案别名、/search 页面
  templates/search.html
  static/app.css
scripts/
  init_db.py           # 建表 + 种子数据
  init_qdrant.py       # 幂等建集合（Cosine，size = EMBEDDING_DIM）
  backfill_index.py    # 幂等回填（第二次不重复切片、不重复嵌入）
  qdrant_stub.py       # Qdrant 最小 REST 子集测试替身（无 Docker 时用）
  validate_spec.py     # 规约自检（本地等价 openspec validate --strict）
  check_compose.py     # compose / Dockerfile 静态校验
tests/
  test_acceptance.py           # 场景级：FR-01~05 / NFR-01~03
  http_acceptance.py           # 真实 HTTP + SQL 计数：A1–A12
  test_chunking.py             # 切分与坐标单元测试
  test_retrieval.py            # 检索（三模式/RRF/阈值/隔离）单元测试
  test_qa.py                   # 问答（上下文/system 丢弃/引用顺序）单元测试
  test_search_page.py          # 页面渲染纪律（转义/清空/三态/409/向量不落 DOM）
  http_retrieval_acceptance.py # 本变更的真实 HTTP 验收（含课件清单逐条）
  harness.py · stubs.py        # 夹具：临时库 + 替身网关/向量库
openspec/
  config.yaml
  changes/add-auth-rbac-class-knowledge/     # 变更一（已实现）
  changes/add-traceable-vector-retrieval/    # 变更二（已实现）
  specs/traceable-vector-retrieval/          # 归档位置（全部验收通过后才写入）
Dockerfile · docker-compose.yml · entrypoint.sh · .env.example
data/app.db            # 运行时生成
uploads/{class_id}/    # 运行时生成
```

## 数据库

六类核心结构：`classes`（班级）、`users`（用户）、`handouts`（讲义）、`assignments`（作业）、`assistants`（助手）、`skills`（技能）；
班级私有数据：`materials`（材料）、`knowledge_entries`（知识库条目，与材料同事务写入，均带 `class_id`）。

本变更新增：

- `knowledge_chunks`（检索用切片索引，**派生数据**，可由 `materials` + `knowledge_entries` 重建；
  含 `start_offset` / `end_offset` / `line_start` / `line_end` / `chunk_sha256` / `source_sha256` / `index_status` / `section`）；
- `materials` 五列：`source_sha256`、`indexed_at`、`index_status`、`chunk_strategy`、`chunk_config_json`。

迁移是幂等的（`db.migrate()`）。回退也安全：`knowledge_chunks` 是派生表，`DROP TABLE` 后既有行为不受影响。
`index_status` 走状态机：`pending → indexing → indexed / failed`；`failed` 表示**向量未写入**
（正文仍可被 `keyword` 命中，向量路不可用），不是「材料丢失」。

## 验收

```bash
# 变更一（回归）
python tests/test_acceptance.py    # 场景级：FR-01~05 / NFR-01~03 每个 Scenario（含 A11 重启留存）
python tests/http_acceptance.py    # 真实 HTTP + SQL 计数：A1–A12

# 变更二（本变更）
python tests/test_chunking.py      # T3 切分与坐标
python tests/test_retrieval.py     # T1/T2/T4/T5/T7 检索链路
python tests/test_qa.py            # T1/T6 问答链路
python tests/test_search_page.py   # T10 页面渲染纪律
python tests/http_retrieval_acceptance.py --list          # 列出全部用例名
python tests/http_retrieval_acceptance.py                 # 真实 HTTP 全套
python tests/http_retrieval_acceptance.py --case ask-no-evidence-no-upstream   # 单条

# 规约与交付物
python scripts/validate_spec.py                       # strict（全部变更）
python scripts/validate_spec.py add-traceable-vector-retrieval --check-coverage
python scripts/check_compose.py

# 有 openspec CLI 的环境应另跑（本机无 CLI）
openspec validate add-traceable-vector-retrieval --strict
```

结果记录见 `ACCEPTANCE.md`（逐条引用 spec 原文）。Docker 与真实 Qdrant 相关项在无 Docker 环境下
以**替身**等价验证，并逐条标注「替身验证 / 真实 Qdrant 待复核」，详见该文件的「验证方式说明」。
