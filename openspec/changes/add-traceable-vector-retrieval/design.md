# Design: add-traceable-vector-retrieval

> 前置：`add-auth-rbac-class-knowledge`（Flask 3.x + SQLite 3 + bcrypt，班级隔离已落地）
> 权威来源：第 4 课课件《登录认证与知识库问答》（2026-09-30 发布）+ `/week04/` 教学原型实测接口契约
> 本文件给出数据结构、切分策略、嵌入与向量库、检索融合、问答链路、索引生命周期、降级语义、
> 安全边界、迁移回退、与课件的偏离、风险取舍。**实施阶段的唯一依据。**

## 1. 决策总览

| # | 决策 | 选择 | 备选 | 理由 |
| --- | --- | --- | --- | --- |
| D1 | 关键字检索实现 | SQLite 原生**字面子串匹配**（`instr()`） | FTS5(+trigram) | SQLite 无 MySQL 的 `FULLTEXT ... WITH PARSER ngram`；二元 ngram 切分在语义上等价于「≤2 字符的连续子串匹配」，用 `instr()` 即可复现且**结果完全确定**；中文无需分词；零新依赖（偏离见 §11） |
| D2 | 向量库 | **Qdrant**，集合 `campusclaw_chunks`，**余弦**距离 | 自建暴力余弦 | 课件已定；元数据过滤可作 **ANN pre-filter**，班级边界在向量侧同样前置 |
| D3 | 向量主键 | **Qdrant point id = `knowledge_chunks.id`** | 自生成 UUID | 与关系库行一一对应，便于教学对照（`/api/materials/:id/vectors`）与幂等重建 |
| D4 | 正文存放 | **只在关系库**（`knowledge_chunks.body_text`）；Qdrant 只存向量 + payload 标识 | 向量库存正文 | 课件口径：「摘录**始终取自 MySQL 的 `chunk_text`**，不取自向量库」；避免双写正文带来的一致性风险 |
| D5 | 切分策略 | `auto` / `custom` / `hierarchy` 三种，**默认 `auto`** | 单一固定策略 | 课件三种策略均有明确规则（见 §3） |
| D6 | 偏移坐标 | **Unicode 码点下标**，相对**预处理后文本**，**左闭右开** | 字节偏移、UTF-16 偏移 | 码点坐标与 Python `str` 索引一致；UTF-16 会因 emoji 错位 |
| D7 | 预处理与原文 | 预处理**不改写原文**：`materials`/`knowledge_entries` 保留上传原文；切片偏移相对**预处理后文本** | 就地改写正文 | 课件口径；原文是审计基线，不可被检索优化污染 |
| D8 | 归一化 | 入库时对正文做 **NFC**；匹配时两侧**仅折叠 ASCII 大小写** | 全量 casefold | ASCII casefold **长度不变** → 偏移恒精确 |
| D9 | 切片写入时机 | 切片与 `materials`/`knowledge_entries` **同事务**；**向量写入在提交后**，失败置 `index_status='failed'` 但**保留材料记录** | 全部同事务 | 课件：「嵌入失败时材料记录仍保留，对应切片标记为 failed，不写入不完整向量」 |
| D10 | 混合融合 | **RRF，k = 60**；两路命中 `1/(60+rank_kw) + 1/(60+rank_vec)`，单路 `1/(60+rank)`；**缺席的一路不贡献分数**（不是分数相加） | 加权求和、分数归一化 | 课件已定；RRF 对两路分数量纲不敏感，无需调权 |
| D11 | 向量阈值 | **余弦 < 0.35 的候选丢弃**，且**不进入向量结果**（含 hybrid 的向量一路） | 全量返回再截断 | 课件已定；避免弱相关候选污染引用可追溯性 |
| D12 | 问答生成 | **先检索再生成**：混合模式取本班**前 4 条**；**0 条则不调用对话网关**，直接返回固定文案 | 无依据时仍调模型要求它说"不知道" | 课件三条备选均被否决，理由见课件「三条备选方案的取舍」 |
| D13 | system 提示 | **由服务端写入**；客户端传入的 `system` 消息**一律丢弃** | 信任客户端 system | 课件：「存在命中切片时，system 提示由服务端写入，仅允许模型依据编号对应的资料作答」 |
| D14 | 问答形态 | **一次性简短回答**（带 `[1]` 标注） | 流式输出 | 课件：只有**流式长对话**留给第 5 课 |

## 2. 数据结构

### 2.1 新增表 `knowledge_chunks`（班级私有）

```sql
-- 检索用的分块索引（派生数据，可由 materials + knowledge_entries 重建）
CREATE TABLE IF NOT EXISTS knowledge_chunks (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,   -- 同时是 Qdrant point id（D3）
  material_id    INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
  class_id       INTEGER NOT NULL REFERENCES classes(id),
  chunk_index    INTEGER NOT NULL,          -- 材料内切片序号，0 起（对外字段名 chunk_index）
  body_text      TEXT    NOT NULL,          -- 预处理后、NFC 归一化的切片正文（自身即坐标系）
  start_offset   INTEGER NOT NULL,          -- 相对预处理后文本的码点偏移（含）
  end_offset     INTEGER NOT NULL,          -- 相对预处理后文本的码点偏移（不含）
  line_start     INTEGER NOT NULL,          -- 1 起，左闭
  line_end       INTEGER NOT NULL,          -- 1 起，右闭（含末行）
  char_count     INTEGER NOT NULL,
  chunk_sha256   TEXT    NOT NULL,          -- body_text 的 sha256（向量幂等键）
  source_sha256  TEXT    NOT NULL,          -- 生成该切片时材料预处理文本的 sha256（过期检测）
  index_status   TEXT    NOT NULL DEFAULT 'pending', -- pending|indexed|failed（切片级）
  created_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_class    ON knowledge_chunks(class_id, material_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_chunks_material ON knowledge_chunks(material_id);
```

> `class_id` 冗余存储（可从 `materials.class_id` 推导）是**刻意的**：让班级过滤成为索引可命中的**单表谓词**，检索无需 JOIN 即可施加边界，也使向量侧的 `class_id` payload 过滤有权威来源。

### 2.2 `materials` 扩展列

```sql
ALTER TABLE materials ADD COLUMN source_sha256      TEXT;                              -- 当前预处理文本哈希
ALTER TABLE materials ADD COLUMN indexed_at         TEXT;                              -- 最近索引完成时间
ALTER TABLE materials ADD COLUMN index_status       TEXT NOT NULL DEFAULT 'pending';   -- pending|indexed|stale|failed
ALTER TABLE materials ADD COLUMN chunk_strategy     TEXT NOT NULL DEFAULT 'auto';      -- auto|custom|hierarchy
ALTER TABLE materials ADD COLUMN chunk_config_json  TEXT;                              -- 该材料实际生效的切分参数
```

> 迁移脚本 MUST 容忍「列已存在」（先查 `PRAGMA table_info(materials)`，存在则跳过），见 §10。

### 2.3 向量库记录（Qdrant point）

```
collection : campusclaw_chunks
distance   : Cosine
size       : EMBEDDING_DIM（来自环境变量，与模型一致）
id         : knowledge_chunks.id            （D3）
vector     : float[EMBEDDING_DIM]           （无归一化要求；余弦距离由 Qdrant 计算）
payload    : { chunk_id, material_id, class_id, chunk_index, chunk_sha256 }
```

payload **MUST NOT** 包含 `body_text`（D4）。`class_id` 必须入 payload，作为 ANN 检索的 **pre-filter** 参数；`chunk_sha256` 作为**幂等键**：同一 `chunk_sha256` 重复写入 MUST 得到同一条点，不产生重复。

## 3. 切分策略

### 3.1 预处理（不改写原文）

预处理是**索引侧**的临时视图，作用对象是关系库里的原文副本，**回写原文的动作一律禁止**：

1. **换行归一化**：`\r\n` / `\r` → `\n`（与 `knowledge.parse_file` 一致）。
2. **空白折叠**（仅当 `collapse_whitespace=true`）：连续空白折叠为单个空格，**但保留 `\n`**（否则行号失去意义）。
3. **链接清洗**（仅当 `strip_links=true`）：移除 URL 与邮箱。
4. **NFC 归一化**：对全文做 Unicode NFC；**此后的文本即坐标系本身**，`start_offset` / `end_offset` 均基于它。

> `body_text`（原始正文）保持上传原文不变；切分偏移是「相对**预处理后**文本」，响应中 MUST 通过 `offset_basis` 字段声明这一点。

### 3.2 三种策略

| 策略 | 规则 | 关键参数 |
| --- | --- | --- |
| **`auto`（默认）** | 优先在**空行**断开；段落内再按**换行**断开；仍超长则按**句号**（`。！？.!?`）断开；最后按窗口硬切 | `max_chars=800`、`overlap_percent=10`（即重叠 80） |
| **`custom`** | 按用户给定的**分隔符**切分（缺省为连续空行），再按窗口硬切 | `separator`（可选）、`max_chars ∈ [100, 2000]`、`overlap_percent ∈ [0, 50]`、`collapse_whitespace`、`strip_links` |
| **`hierarchy`** | 按 Markdown 标题层级 `#` / `##` / `###` 分章（记录章节路径）；单章过长再按窗口切 | 继承 `max_chars` / `overlap_percent` |

**窗口硬切规则**：自 `max_chars` 处硬切，起点回退 `overlap`（= `max_chars × overlap_percent / 100`，向下取整）个码点；**重叠必须小于块长**，否则强制 `overlap = max(0, max_chars // 5)` 并记录一条 warning。

**过短合并**：相邻两片若合并后 ≤ `max_chars` 且其中一片 < 32 码点，则合并。

### 3.3 坐标与哈希

- `start_offset` / `end_offset`：相对**预处理后文本**的**码点**偏移，**左闭右开**。
- `line_start` / `line_end`：LF 归一化后的行号，**1 起**，`line_end` **含**末行。
- `chunk_sha256 = sha256(body_text)`；`source_sha256 = sha256(预处理后全文)`；`materials.source_sha256` = 同一值。
- **确定性**：同一正文 + 同一参数重复切分 MUST 得到完全相同的切片划分与坐标。

### 3.4 边界行为（写入 spec 作为验收依据）

| 边界 | 行为 | 说明 |
| --- | --- | --- |
| **跨切片边界的关键词** | **不命中** | 匹配在**片内**进行，不跨片拼接；避免"跨片命中后偏移无法归属单一片" |
| **emoji** | 按码点索引 | 硬切允许落在码点边界而非图形簇边界；MVP 不保证 grapheme cluster 完整 |
| **CRLF** | 归一化后计行号 | `\r\n` 与 `\n` 得到**相同**行号 |
| **空文件 / 全空白** | 不产生切片 | 材料 `index_status='failed'`，检索时计入 `index_warnings` |
| **超长无空行正文** | 按 `max_chars` 硬切 | 片数与 `ceil(N / (max_chars - overlap))` 相关 |
| **组合字符** | NFC 后可能改变长度 | 故归一化必须在入库时完成（存储即坐标系）；查询串同样先 NFC |

## 4. 嵌入与向量库

### 4.1 课程网关调用（**密钥只在服务端**）

| 用途 | 请求 | 约束 |
| --- | --- | --- |
| 嵌入 | `POST {EMBEDDING_BASE_URL}/embeddings`，body 只含 `model` 与 `input` | `data[0].embedding` 长度 MUST 等于 `EMBEDDING_DIM`，否则判为失败并**不写入向量** |
| 对话 | `POST {CHAT_BASE_URL}/v1/chat/completions`，用对话模型名 | **仅在有命中切片时调用**（D12） |

- `EMBEDDING_BASE_URL` 只写到 `/v1`。
- 所有请求在**服务端**发出；浏览器**不持有**任何网关密钥，也无法直连 Qdrant（§9）。
- 单次请求体不超过 20MB；超限时按批切分（`input` 支持数组，批量 ≤ 64 条/次）。
- 网关**超时**：嵌入 15s、对话 30s；超时按失败处理（嵌入 → `index_status='failed'`；对话 → `502 upstream_error`，**不返回编造答案**）。

### 4.2 写入 / 删除 / 重建

| 场景 | 动作 |
| --- | --- |
| 上传成功 | 提交后：批量嵌入 → `upsert` 到 `campusclaw_chunks`（id = chunk_id） |
| **重建**（`reindex`） | **先删旧向量与旧切片，再写入新切片与新向量**（课件：「换策略必须显式重建索引」） |
| 材料删除 | 按 payload filter `material_id` 删除点（当前无删除接口，预留） |
| 幂等 | 以 `chunk_sha256` 为幂等键；已存在且哈希一致的点跳过嵌入，不重复计费 |

### 4.3 失败处理

- 嵌入失败 → 该材料 `index_status='failed'`，**材料记录保留**，对应切片 `index_status='failed'`，**不写入不完整向量**（D9）。
- 部分切片成功 → 材料 `index_status='stale'`，检索时计入 `index_warnings`。
- 集合不存在 → 启动或首次写入时调用 `scripts/init_qdrant.py` 建集合；建集合失败不阻塞服务启动。

## 5. 检索

### 5.1 三种模式

| 模式 | 数据源 | 是否调嵌入 | 是否访问 Qdrant | 备注 |
| --- | --- | --- | --- | --- |
| `keyword` | 关系库 | **否** | **否** | 只查 `index_status='indexed'` 的切片 |
| `vector` | Qdrant | 是 | 是 | 余弦 < 0.35 丢弃（D11） |
| `hybrid` | 两路 | 是 | 是 | 两路各自过滤 → RRF k=60（D10） |

### 5.2 候选生成与班级过滤位置（**最关键的实现约束**）

班级过滤 MUST 出现在**两条路径的候选生成之前**，并在匹配、计数、排序、top-k/limit **之前**：

```sql
-- 关键字路径（顺序不可调整）
SELECT c.id, c.material_id, c.chunk_index, c.body_text, c.start_offset, c.end_offset
  FROM knowledge_chunks c
 WHERE c.class_id = ?                                   -- ① 班级边界：最先、单表、索引可命中
   AND c.index_status = 'indexed'                       -- ② 只检索就绪切片
   AND instr(lower_ascii(c.body_text), lower_ascii(?)) > 0  -- ③ 匹配：在班级范围内
 ORDER BY /* 见 5.4 */                                  -- ④ 排序：只对本班候选
 LIMIT ?;                                                -- ⑤ 上限：最后施加
```

```
-- 向量路径（Qdrant）
search(collection=campusclaw_chunks,
       vector=query_vector,
       filter={ must: [ { key: "class_id", match: { value: <会话班级> } } ] },  -- ① pre-filter
       limit=top_k, score_threshold=0.35)                                        -- ② 阈值
-- ③ 回表关系库取正文时，MUST 再次带 class_id 条件（双保险）
```

**明确禁止**：

- ❌ 先全库检索、再在服务端或前端丢弃他班行（"检出后过滤"）；
- ❌ 先全库排序取 top-k、再过滤班级（会挤掉本班结果，并泄露他班存在性）；
- ❌ 向量检索不带 `class_id` pre-filter，指望回表时再筛；
- ❌ 用前端隐藏作为隔离手段。

> **反向判据**：把 MySQL/SQLite 侧的 `class_id` 谓词**与** Qdrant 的 `class_id` filter 各自单独移除，验收用例（跨班同关键词）MUST 立刻失败 —— 证明两条路径都在承担隔离职责。课件口径：「即使向量库漏过滤，回表时的班级条件仍应排除其他班级」——本设计**要求两层都带**，任一层失效仍不泄露。

### 5.3 RRF 融合与阈值

```
score_final(chunk) =
  Σ over 命中路径 P ∈ {keyword, vector}:
      1 / (60 + rank_P(chunk))      -- 仅在该路径命中时累加；缺席的一路不贡献分数
```

- `rank_P` 为该方法内的**名次，1 起**。
- **不**把两路的原始分数相加（两者量纲不同）。
- `hybrid` 下，余弦 < 0.35 的切片**不进入向量结果**，因此也不参与 RRF（D11）。
- 单路命中：`score_final = 1 / (60 + rank)`（等价于该路单独贡献）。
- 排序键：`score_final DESC` → `material_id ASC` → `chunk_index ASC`（**稳定次序**，同分可复现）。
- `score_keyword` / `score_vector` / `rank_keyword` / `rank_vector` MUST 在响应中分别给出（未命中的一路为 `null`），便于教学核对公式。

### 5.4 分页与上限

- 请求：`{q, mode, limit, offset}`；原型用 `limit=10, offset=0`。
- `limit` 默认 `10`，范围 `1..20`；`offset` 默认 `0`，范围 `0..1000`；越界 → `400 invalid_query`（**不静默截断**）。
- `keyword` 模式下 `score_*` 仍按 `1/(60+rank)` 计算，使三模式字段形状一致。

### 5.5 状态与降级

| 状态 | 判定 | 响应 |
| --- | --- | --- |
| `ok` | 至少 1 条命中 | `200` + `status:"ok"` + `hits[]` |
| `no_evidence` | 本班索引完好，0 条命中 | `200` + `status:"no_evidence"` + `hits:[]` + `message:"资料中未找到相关内容"` |
| 降级 | 本班存在 `index_status != 'indexed'` 的材料 | `200` + `degraded:true` + `index_warnings:[{material_id,status}]` + **已索引部分的结果**（`status` 仍按是否有命中给出） |
| Qdrant 不可用 | `vector` / `hybrid` 模式 | **`503 vector_store_unavailable`**，**不编造相似度分数**；`keyword` 模式**仍正常返回** |
| 网关不可用 | 需要嵌入的模式 | `503 embedding_unavailable`；`keyword` 模式不受影响 |
| 对话网关不可用 | `/api/ask` 且已有命中 | `502 upstream_error`，**不返回编造答案** |
| 数据库故障 | 异常 | `503`，响应体不含任何材料内容 |

> **索引缺失或过期 MUST NOT 伪装成「没有结果」。** 判据：`degraded:true` 时即使 `hits` 为空，也 MUST 带 `index_warnings`。

## 6. 问答（`POST /api/ask`）

### 6.1 流程

```
1. 鉴权：无会话 → 401
2. 校验：q 空 → 400 invalid_query
3. 检索：以 hybrid 模式、**本班**、**前 4 条**（top_k = 4）取回切片
4. 0 条 → 直接返回 200 { answer: "资料中未找到相关内容", status: "no_evidence", citations: [] }
        **此路径 MUST NOT 调用对话网关**（D12）
5. ≥1 条 → 组装上下文 → 调用对话网关 → 返回 { answer, status: "ok", citations: [...] }
```

> 只取**最新一句**作检索输入（课件：`问题仅以最新一句发起检索`）。`history` 仅附于对话上下文之后，**不参与检索**。

### 6.2 上下文组装（**只有服务端能构造**）

传给对话模块的，**仅限**：

| 传入 | 不传 |
| --- | --- |
| 材料标题 | 向量分量、Qdrant 原始点数据 |
| 切片序号（`chunk_index`） | 其他班级的切片 |
| 切片正文 | **客户端自行构造的 `system` 消息** |
| 用户最新一句提问 | |
| 此前若干轮对话（附于其后） | |

### 6.3 system 提示

- **由服务端写入**，内容约束为：仅允许依据编号对应的资料作答；资料不足以回答时明确说明，不得依据记忆补全。
- 客户端请求体 `history` 中出现的任何 `role == "system"` 消息 MUST 被**丢弃**（D13）。
- 客户端**不能**通过任何请求字段替换或追加上下文中的切片内容。

### 6.4 无命中时的对外行为

- 返回 **HTTP 200**（**不是 404，也不是 403**），`answer` 为固定文案「资料中未找到相关内容」，`citations` 为 `[]`。
- 该固定文案由接口**在检索之后直接返回**，不经模型。
- 理由（课件否决了三条备选）：提示约束可能被忽略；404 会把空结果与资源不存在混为一谈；弱相关候选会降低可追溯性。

### 6.5 `citations` 与 `[n]` 的顺序一致性

- `answer` 中的 `[1]` `[2]` … 与 `citations[]` 的**下标顺序 MUST 一致**（`[1]` 对应 `citations[0]`）。
- `citations[i]` 字段：`chunk_id, material_title, chunk_index, start_offset, end_offset, snippet`。
- 服务端 MUST 校验：`answer` 中出现的引用编号**不得超过**实际传入的切片数；越界引用在返回前被剔除，并记一条 warning。

## 7. API 契约（对齐课件 §4）

| 端点 | 请求 | 响应要点 |
| --- | --- | --- |
| `POST /api/login` | `{username, password}` | Cookie 会话；失败同形（不泄露用户名是否存在） |
| `POST /api/logout` | — | 会话失效 |
| `GET /api/me` | — | 会话状态（`user_id` / `role` / `class_id` / `class_name`） |
| `POST /api/search` | `{q, mode, limit, offset}` | `{query_vector, hits[], status, degraded, index_warnings[], message?}`；`status === "no_evidence"` 时附 `message` |
| hit 字段 | — | `chunk_id, material_id, material_title, chunk_index, start_offset, end_offset, snippet, score_keyword, score_vector, rank_keyword, rank_vector, score_final` |
| `POST /api/ask` | `{q, history}` | `{answer, status, citations[]}`；citation：`chunk_id, material_title, chunk_index, start_offset, end_offset, snippet` |
| `POST /api/materials/<id>/reindex` | `{chunk_strategy, chunk_separator, chunk_max_chars, chunk_overlap_percent, collapse_whitespace, strip_links}` | 重建该材料切片与向量 |
| `GET /api/materials/<id>/vectors` | — | 对照关系库行与向量库记录（教学用） |
| `GET /api/materials` | `page`、`page_size=20`、`title` | 本班材料列表（分页） |
| 上传 | `multipart/form-data`，`file` + 上述切分参数 | 返回 `title` / `chunk_strategy` / `chunk_count` |

### 7.1 状态码约定（全项目统一）

| 情形 | 状态码 | 响应体 |
| --- | --- | --- |
| 无会话 | `401 unauthorized` | `{"error":"unauthorized",...}` |
| 角色不符（如学生调用 `reindex`） | `403 forbidden` | 含 `required_role`、`your_role` |
| `q` 空/超长/含控制字符、`limit`/`offset` 越界 | `400 invalid_query` | 含具体违反项 |
| 跨班或不存在（材料详情 / 来源 / vectors） | `404 not_found` | **两者字节级一致** |
| 来源版本过期 | `409 source_stale` | 不含正文 |
| Qdrant 不可用（`vector` / `hybrid`） | `503 vector_store_unavailable` | 不含编造分数 |
| 对话网关不可用 | `502 upstream_error` | 不含编造答案 |
| 数据库故障 | `503` | 不含任何材料内容 |
| **跨班检索** | **`200` + 空 `hits`** | **MUST NOT 用 403/404 暗示资料属于其他班级** |

### 7.2 兼容别名（早期草案端点）

`GET /api/knowledge/search` 与 `GET /api/knowledge/sources/<chunk_id>` **保留**，指向同一实现；
其响应保留草案期的字段名（`title` / `char_start` / `char_end` / `line_start` / `line_end` /
`snippet_char_start` / `source_url` / `download_url` / `coordinate_system`），以免草案期已写下的验收判据失效。
**两个端点同样受班级隔离与鉴权约束。**

## 8. 索引生命周期与 `index_status` 状态机

### 8.1 材料级 `materials.index_status`

```
pending ──(切片写入成功)──> indexed ──(正文变更/重建中)──> stale
   │                            │
   └──(解析或切片失败)──────────┴──> failed
```

| 状态 | 含义 | 检索时 |
| --- | --- | --- |
| `pending` | 已入库、尚未建索引 | 计入 `index_warnings` |
| `indexed` | 切片与向量均就绪 | 正常参与检索 |
| `stale` | 切片就绪但向量缺失/部分失败，或正文已变更 | 计入 `index_warnings`；**关键字路径仍可用** |
| `failed` | 正文为空、解析失败或嵌入失败 | 计入 `index_warnings`；不参与检索 |

### 8.2 切片级 `knowledge_chunks.index_status`

`pending`（已写切片未写向量）→ `indexed`（向量就绪）/ `failed`（嵌入失败）。
**关键字路径只检索 `indexed` 的切片吗？** —— **不是**：关键字路径只要求 `materials.index_status` 属于可用集合，
切片级状态仅用于**决定该切片是否进入向量路径**。判据：若关键字路径也排除了 `pending` 切片，
「嵌入失败但正文可检索」这一课件要求就落不了地。

### 8.3 各阶段

| 阶段 | 触发 | 行为 |
| --- | --- | --- |
| **建** | 教师上传成功 | 同事务写 `materials` + `knowledge_entries` + `knowledge_chunks`（`index_status='pending'`）；提交后嵌入 → 置 `indexed` / `failed` |
| **回填** | `scripts/backfill_index.py` | 幂等：`materials.source_sha256` 已等于当前哈希且 `index_status='indexed'` → **跳过**；否则删旧切片、重建、写向量（以 `chunk_sha256` 为幂等键） |
| **重建** | `POST /api/materials/<id>/reindex` 或 `backfill_index.py --force` | **先删旧切片与旧向量，再写入新的**；换切分策略 MUST 走此路径（课件） |
| **检测** | 每次检索 | 比对 `materials.source_sha256` 与切片 `source_sha256`；不一致或状态非 `indexed` → `index_warnings` + `degraded=true` |
| **持久化** | 容器重启 / `down` + `up` | 切片在 SQLite（随 `./data` 卷）；向量在 Qdrant（随其自身卷）。**无需重启后全量重建** |
| **删除** | 材料被删 | 切片走 `ON DELETE CASCADE`；向量按 payload filter 删除 |

## 9. 安全边界

1. **授权只来自会话**：`class_id`、`role` 只取 `session`；query / body / header 中的 `class_id`、`class`、`role`、`user_id` 一律**丢弃**（课件：「解析后一律丢弃」「查询参数一律无效」）。
2. **班级过滤在最前**：见 §5.2；MySQL/SQLite 与 Qdrant **两条路径都要带**会话班级。
3. **禁止检出后过滤**：禁止全库检索后隐藏，禁止先全库排序再筛班级。
4. **不泄露他班**：结果、计数、`index_warnings`、错误信息均不含他班标题、正文、路径、计数；**跨班检索对外是 `200` + 空 `hits`**，不得用 403/404 暗示资料属于其他班级。
5. **404 同形**：跨班来源与不存在来源**字节级相同**（沿用上一变更固定 404 的取舍）。
6. **密钥只在服务端**：`EMBEDDING_API_KEY` / `QDRANT_API_KEY` 只从环境变量注入，**不下发浏览器**；浏览器网络面板中不可见；客户端无法直连 Qdrant 与模型网关。
7. **前端存储纪律**：`localStorage` **只存主题**，不存任何 token / 会话标识。
8. **前端转义**：查询词回显、`material_title`、`snippet` 一律转义后写入 DOM（`textContent` 或模板自动转义）；**禁止** `innerHTML` 拼接。片段可含上传的 `<script>`，必须按文本渲染。
9. **模型输入不可被客户端污染**：上下文与 `system` 均由服务端构造（§6.2/§6.3）。
10. **输入即数据**：`q` 只作参数化查询的值，绝不拼接进 SQL；也不作为正则求值（避免 ReDoS）。
11. **会话失效清理**：收到 401/403 时前端 MUST 清空上一次的 `hits`、`citations` 与查询回显。

## 10. 迁移与回退

**迁移（向前，幂等）**

1. `db.SCHEMA_SQL` 追加 `knowledge_chunks` 建表 + 索引（`CREATE TABLE/INDEX IF NOT EXISTS`）。
2. `materials` 五列用 `ALTER TABLE ... ADD COLUMN`，实施时先查 `PRAGMA table_info(materials)`，**列已存在则跳过**。
3. 既有行不受影响：新列可为 `NULL`；`index_status` 默认 `'pending'`，`chunk_strategy` 默认 `'auto'`。
4. 迁移后执行 `scripts/backfill_index.py` 为历史材料建切片与向量；**未回填前检索返回 `degraded:true`，不返回假"无命中"**。
5. Qdrant 集合由 `scripts/init_qdrant.py` 建立（幂等）；未建集合时 `vector` / `hybrid` 走降级语义，`keyword` 不受影响。

**回退（向后，低风险）**

- `knowledge_chunks`、`materials` 五个新列、Qdrant 集合均为**派生数据**，可由 `materials` + `knowledge_entries` **完整重建**。
- 回退：`DROP TABLE knowledge_chunks;` + 删除 Qdrant 集合 + 忽略新列（保留不影响既有查询）。
- **不需回退**（不被本变更触碰）：`classes`、`users`、`materials` 既有列语义、`knowledge_entries`、上传/下载/登录/会话、Docker 启动方式、`class_id` 语义。
- 回退后既有 23 + 58 项验收 MUST 仍全绿（本变更为**增量**）。

## 11. 与课件参考实现的偏离（显式声明）

课件参考实现为 **Go + MySQL 8 + Qdrant**。本组沿用第 3 课已验收的 **Flask 3 + SQLite 3**，仅向量库与课件一致。

| # | 课件 | 本实现 | 偏离理由 | 对外行为是否一致 |
| --- | --- | --- | --- | --- |
| 1 | 后端语言 Go | Python 3 + Flask 3 | 第 3 课已交付并通过 23 + 58 项验收；重写会丢弃已验证成果并引入新的回归面 | **一致**：接口路径、字段、状态码、Cookie 会话语义完全相同 |
| 2 | 关系库 MySQL 8 | SQLite 3（`node:sqlite` 无，使用 Python 内置 `sqlite3`） | 单机教学场景，无需独立数据库服务；Docker Compose 启动更简单 | **一致**：SQL 语义与事务行为等价 |
| 3 | `FULLTEXT ... WITH PARSER ngram`，token 长度 2 | `instr()` 字面子串匹配 | SQLite 无 ngram 解析器。**二元 ngram 检索的本质是「长度 ≤2 的连续子串匹配」**，`instr()` 在语义上覆盖同一目标：中文连续子串可直接命中，无需分词 | **等价**（见下方等价性论证） |
| 4 | 集合 `campusclaw_chunks`、余弦 | **同** | — | **一致** |
| 5 | 阈值 0.35、RRF k=60、前 4 条 | **同** | — | **一致** |
| 6 | `GET /api/materials` 分页 | 同（默认 `page=1, page_size=20`），并在既有响应上**追加** `page` / `page_size` / `total` | 兼容第 3 课已验收的响应形状 | **一致**（向后兼容） |
| 7 | — | **保留**第 3 课既有端点与草案别名端点 | 维持既有验收判据有效 | **超集**，不影响课件契约 |

**ngram 与 `instr()` 的等价性论证**（偏离 3）

- MySQL ngram 解析器把文本切成**连续 n 元组**（此处 n=2），查询串同样切分，匹配的是「查询串的每个二元组是否都出现在文档中」。
- 因此它**同样无法匹配跨词/跨序的语义**，也**不做分词或同义词**；它的召回面是「连续子串」这一族。
- `instr(body, q)` 精确判定「`q` 是否为 `body` 的连续子串」；对中文而言这与二元 ngram 的召回目标一致，且在**边界上更严格**（要求整串连续，而非各二元组各自出现）。
- **差异点（如实记录）**：单字符查询 `q`（长度 1）在 ngram 下通常因 token 长度 2 而不被索引，在 `instr()` 下则可命中。本实现**允许**单字符查询，并在 spec 中把它列为**能力超出**而非缺失。
- **反向差异**：ngram 对「二元组各自命中但顺序不同」可能召回，`instr()` 不召回。本实现召回面更窄但**结果更确定**，且与「可溯源」目标一致（能给出唯一偏移）。

## 12. 风险与取舍

| 风险 / 取舍 | 影响 | 处理 |
| --- | --- | --- |
| **跨切片边界的关键词不命中** | 长词跨段时查不到 | 已明确为**已知行为**并写入 spec 场景；可通过调大 `max_chars` 缓解 |
| **无分词 / 无同义词 / 无拼音** | 「方程解法」查不到「解方程」 | 属"可复现"的代价；向量模式可部分弥补语义召回 |
| **全角与半角不互通** | 全角 `Ａ` 不匹配半角 `A` | 已知取舍；可加 NFKC 匹配层（需引入偏移映射表，未纳入本变更） |
| **向量结果受模型版本影响** | 换模型后 `score_vector` 与排序可能变化 | `EMBEDDING_MODEL` / `EMBEDDING_DIM` 写入环境变量与文档；换模型 MUST 全量重建（`backfill_index.py --force`） |
| **嵌入调用成本与配额** | 按每日 token 额度计费 | 幂等键 `chunk_sha256` 跳过重复嵌入；批量 ≤ 64 条/次 |
| **Qdrant 与关系库双写** | 可能不一致 | 关系库为权威源；Qdrant 为派生数据；检索回表必带 `class_id` 双保险；不一致由回填脚本修复 |
| **改写既有上传事务** | 触碰已验收代码有回归风险 | 上传路径仅**追加**切片写入与提交后嵌入；既有断言（行数、class 一致性、无脏数据）MUST 保持全绿 |
| **「嵌入失败仍保留材料」与「上传失败无脏数据」的边界** | 两条契约看似冲突 | 明确切分：**解析失败** → 整体回滚无脏数据（DDL 级）；**解析成功但嵌入失败** → 材料保留、状态 `failed`、不写不完整向量（课件口径）。两者以「材料记录是否存在」为判据，互不矛盾 |
| **无 openspec CLI 时的校验** | 本地无 `openspec` 命令 | 用仓库内 `scripts/validate_spec.py` 作**等价校验**并如实记录；有 CLI 的环境应补跑 `openspec validate add-traceable-vector-retrieval --strict` |
| **本机无 Docker，Qdrant 难以本地起** | `vector` / `hybrid` 路径无法端到端验证 | 提供 `scripts/qdrant_stub.py`（实现 Qdrant 最小 REST 子集的**测试替身**，仅本地验收用，非生产组件）；真实 Qdrant 应在 `docker compose up` 环境复核，并在 `ACCEPTANCE.md` 如实标注两者的差别 |
