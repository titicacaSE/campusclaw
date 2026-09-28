# Design: add-class-scoped-knowledge-search

> 前置：`add-auth-rbac-class-knowledge`（Flask 3.x + SQLite 3 + bcrypt，班级隔离已落地）
> 本文件给出数据结构、检索方式、API、索引生命周期、溯源定位、安全边界、取舍与迁移回退。
> **本变更不实施代码**，以下为实施阶段的权威依据。

## 1. 决策总览

| # | 决策 | 选择 | 备选 | 理由 |
| --- | --- | --- | --- | --- |
| D1 | 检索实现 | SQLite 原生**字面子串匹配**（`instr()` / 转义后的 `LIKE`） | FTS5(+trigram)、向量库 | 中文无需分词即可复现；零新依赖；班级级数据量（≤10⁴ 块）性能足够；结果是**完全确定的**，便于验收 |
| D2 | 分块策略 | 段落优先 + 512 码点上限硬切 | 定长滑窗、按标题层级 | 段落边界与教学讲义结构一致，偏移可精确定位到行 |
| D3 | 偏移坐标 | **Unicode 码点下标**，相对**材料全文**（NFC 归一化后） | 字节偏移、UTF-16 偏移 | 码点坐标与 Python `str` 索引一致，避免代理对歧义；UTF-16 会因 emoji 产生错位 |
| D4 | 归一化 | 入库时对正文做 **NFC**；匹配时两侧**仅折叠 ASCII 大小写** | 不归一化、全量 casefold | ASCII casefold **长度不变** → 偏移恒精确；NFC 在入库时完成 → 存储文本即坐标系 |
| D5 | 索引写入 | 与材料/正文**同事务** | 异步队列 / 定时重建 | 满足「一致提交」；失败可整体回滚，无中间态 |
| D6 | 向量数据库 | **不采用**（见 §8） | 语义向量召回 | 本变更范围是「可复现的关键词检索」；引入向量会带来模型、维度、同步机制与语义不确定性，与 Non-goals 冲突 |
| D7 | 溯源读取 | **独立端点**，重新鉴权 + 版本核对 | 结果里内嵌全文 | 内嵌全文会让**过期片段看起来仍然有效**；独立端点可返回 `stale` 信号 |
| D8 | 来源过期 | `409 source_stale`，**不返回正文** | 200 + 标记 | 409 语义精确，前端不可能把过期内容误当有效内容渲染 |

## 2. 数据结构

### 2.1 新增表 `knowledge_chunks`（班级私有）

```sql
-- 检索用的分块索引（派生数据，可由 materials + knowledge_entries 重建）
CREATE TABLE IF NOT EXISTS knowledge_chunks (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  material_id   INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
  class_id      INTEGER NOT NULL REFERENCES classes(id),
  ord           INTEGER NOT NULL,          -- 材料内分块序号，0 起
  body_text     TEXT    NOT NULL,          -- NFC 归一化后的块正文（自身即坐标系）
  char_start    INTEGER NOT NULL,          -- 相对材料全文的码点偏移（含）
  char_end      INTEGER NOT NULL,          -- 相对材料全文的码点偏移（不含）
  line_start    INTEGER NOT NULL,          -- 1 起，左闭
  line_end      INTEGER NOT NULL,          -- 1 起，右闭（含末行）
  char_count    INTEGER NOT NULL,
  hit_count     INTEGER NOT NULL DEFAULT 0,-- 最近一次检索的命中次数（仅用于排序缓存，可为 0）
  chunk_sha256  TEXT    NOT NULL,          -- body_text 的 sha256
  source_sha256 TEXT    NOT NULL,          -- 生成该块时材料全文的 sha256（过期检测）
  created_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_class    ON knowledge_chunks(class_id, material_id, ord);
CREATE INDEX IF NOT EXISTS idx_chunks_material ON knowledge_chunks(material_id);
```

> `class_id` 冗余存储（可从 `materials.class_id` 推导）是**刻意的**：让班级过滤成为索引可命中的**单表谓词**，避免每次检索都依赖 JOIN 才能施加边界。

### 2.2 `materials` 扩展列（预留，实施时写入）

```sql
ALTER TABLE materials ADD COLUMN source_sha256 TEXT;                          -- 当前正文哈希
ALTER TABLE materials ADD COLUMN indexed_at    TEXT;                          -- 最近索引完成时间
ALTER TABLE materials ADD COLUMN index_status  TEXT NOT NULL DEFAULT 'pending'; -- pending|indexed|stale|failed
```

> 迁移脚本 MUST 容忍「列已存在」（重复执行幂等），见 §9。

### 2.3 分块算法（边界明确定义）

输入：材料的纯文本正文（`knowledge_entries.body_text`，与上传解析结果一致）。

1. **换行归一化**：`\r\n` / `\r` → `\n`（与 `knowledge.parse_file` 保持一致）。
2. **NFC 归一化**：对全文做 Unicode NFC；**此后的文本即坐标系本身**，`char_*` 均基于它。
3. **段落切分**：按「连续 ≥2 个 `\n`」切分为段落，丢弃空洞段落。
4. **超长硬切**：段落长度 > 512 码点 → 自 512 处硬切，余下继续，直至每块 ≤ 512。
5. **过短合并**：相邻块长度 < 64 码点且合并后 ≤ 512 → 合并。
6. **记录坐标**：逐块累计 `char_start` / `char_end`（码点、左闭右开）与 `line_start` / `line_end`（行号、1 起、右闭）。
7. **哈希**：`chunk_sha256 = sha256(body_text)`；`source_sha256 = sha256(全文)`。

**明确的边界行为（写入 spec 作为验收依据）：**

| 边界 | 行为 | 说明 |
| --- | --- | --- |
| **跨块边界的关键词** | **不命中** | 匹配在**块内**进行，不跨块拼接。这是确定性取舍（避免"跨块命中后偏移无法归属单一块"） |
| **emoji** | 按码点索引 | Python `str` 逐码点；带 ZWJ / 肤色修饰的序列是**多码点**，硬切时**允许**切在码点边界而非图形簇边界 —— MVP 不保证 grapheme cluster 完整 |
| **CRLF** | 归一化后计行号 | `\r\n` 与 `\n` 得到**相同**行号；正文若含孤立 `\r` 同样先转为 `\n` |
| **空文件 / 全空白** | 不产生块 | `index_status='failed'`，检索时计入 `index_warnings` |
| **超长无空行正文** | 按 512 硬切 | 块数与 `ceil(N/512)` 相关（经合并调整） |
| **组合字符** | NFC 后**可能改变长度** | 故归一化必须在**入库时**完成（存储即坐标系）；查询串同样先 NFC，保证两侧坐标系一致 |

## 3. 检索方式

### 3.1 输入规则（严格，不静默改写）

| 参数 | 规则 | 违反时 |
| --- | --- | --- |
| `q` | 必填；`strip()` 后长度 1..64 **个 Unicode 码点**；不得含 C0/C1 控制字符（`\x00-\x1f`、`\x7f-\x9f`） | `400 invalid_query` |
| `limit` | 可选；整数，默认 `10`，范围 `1..20` | `400 invalid_query`（**不静默截断**，避免"看着正常其实被改写"） |

> `q` 先做 NFC（与入库一致），再按 3.2 匹配。长度检查在 **NFC 之后**进行，因此 1 个含组合字符的"字符"可能占用多个码点。

### 3.2 匹配规则（可复现，无隐藏语义）

- **字面子串匹配**：无分词、无同义词、无模糊、无拼音、无语义扩展。
- **ASCII 字母大小写不敏感**：匹配时仅对 `A-Z`/`a-z` 折叠；**因为折叠不改变长度，偏移恒精确**。
- **非 ASCII 精确匹配**：中文、emoji、全角字符均**区分**（全角 `Ａ` 与半角 `A` **不互相匹配** —— 这是已知取舍，见 §10）。
- **SQL 通配符按字面处理**：`%`、`_`、`\` 视为普通字符；实现上优先用 `instr()`，若用 `LIKE` 必须显式 `ESCAPE`。
- **无正则**：`q` 不作为正则表达式求值（避免 ReDoS）。

### 3.3 候选生成与班级过滤位置（**最关键的实现约束**）

班级过滤 MUST 出现在**候选生成的第一条谓词**上，并在匹配、计数、排序、`top-k/limit` **之前**：

```sql
-- 实现示意（顺序不可调整）
SELECT c.id, c.material_id, c.body_text, c.char_start, c.char_end,
       c.line_start, c.line_end, c.ord
  FROM knowledge_chunks c
 WHERE c.class_id = ?                      -- ① 班级边界：最先、且是索引可命中的单表谓词
   AND instr(lower_ascii(c.body_text), lower_ascii(?)) > 0   -- ② 匹配：在班级范围内
 ORDER BY /* 见 3.4 */                     -- ③ 排序：只对本班候选
 LIMIT ?;                                   -- ④ 上限：最后施加
```

**明确禁止**：

- ❌ 先全库检索、再在服务端或前端丢弃他班行（"检出后过滤"）；
- ❌ 先全库排序取 top-k、再过滤班级（会导致本班结果被挤掉，且泄露他班存在性）；
- ❌ 用前端隐藏作为隔离手段。

> 判定方法：把 `class_id` 谓词从 SQL 中移除，验收用例（跨班同关键词）MUST 立刻失败 —— 说明它确实承担了隔离职责。

### 3.4 排序与上限

1. `hit_count DESC` —— 块内命中次数多者优先
2. `material_id ASC`、`c.ord ASC` —— **稳定次序**，保证同分结果顺序确定、可复现
3. `LIMIT limit`（默认 10，上限 20）

### 3.5 计数

- `total_matched` = **本班**匹配块总数（独立 `COUNT(*)`，条件与 3.3 完全一致）。
- MUST NOT 暴露他班的任何计数、命中数或总数。

### 3.6 三种状态必须可区分

| 状态 | 判定 | 响应 |
| --- | --- | --- |
| **无命中** | 本班索引完好，`total_matched == 0` | `200` + `hit:false` + `results:[]` + `degraded:false` |
| **降级（索引缺失/过期）** | 存在 `index_status IN ('pending','stale','failed')` 的本班材料 | `200` + `degraded:true` + `index_warnings:[{material_id,status}]` + **已索引部分的结果** |
| **故障** | DB 不可读等异常 | `503`（与 `/health` 语义一致），响应体不含任何材料内容 |

> **索引缺失或过期 MUST NOT 伪装成"没有结果"。** 判据：`degraded:true` 时即使 `results` 为空，也必须带 `index_warnings`，前端提示"部分材料尚未建立索引"。

## 4. API

### 4.1 `GET /api/knowledge/search`

请求：`GET /api/knowledge/search?q=<关键词>&limit=<1..20>`

响应 `200`：

```json
{
  "query": "一元二次方程",
  "class_id": 2,
  "role": "teacher",
  "hit": true,
  "total_matched": 3,
  "returned": 3,
  "limit": 10,
  "degraded": false,
  "index_warnings": [],
  "coordinate_system": {
    "unit": "unicode_code_point",
    "normalization": "NFC",
    "offset_base": "material_full_text",
    "snippet_object": "knowledge_chunk.body_text"
  },
  "results": [
    {
      "chunk_id": 12,
      "material_id": 3,
      "title": "B 班数学讲义：一元二次方程解法（第 1 讲）",
      "snippet": "…求根公式的推导…",
      "snippet_char_start": 118,
      "char_start": 96,
      "char_end": 240,
      "line_start": 8,
      "line_end": 15,
      "match_in_chunk": 22,
      "hit_count": 2,
      "chunk_sha256": "…",
      "source_sha256": "…",
      "source_url": "/api/knowledge/sources/12",
      "download_url": "/api/materials/3/download"
    }
  ]
}
```

**溯源字段语义（MUST 逐条写明，供验收比对）：**

| 字段 | 语义 |
| --- | --- |
| `char_start` / `char_end` | **相对材料全文**（NFC 后）的 **Unicode 码点**偏移，**左闭右开** |
| `line_start` / `line_end` | LF 归一化后的行号，**1 起**，`line_end` **含**末行 |
| `snippet` | **摘要对象 = `knowledge_chunk.body_text`（块正文）**，不是材料全文、不是 `knowledge_entries` 整行 |
| `snippet_char_start` | `snippet` 首字符在**材料全文**中的码点偏移（把片段映射回全文坐标） |
| `match_in_chunk` | 命中位置在**块内**的码点偏移（渲染高亮用） |
| `source_url` | 鉴权来源链接，**相对路径**，由服务端路由生成 |
| `download_url` | 复用既有下载端点，**相对路径** |
| `chunk_sha256` / `source_sha256` | 供第三方核对片段与来源版本未被篡改 |

> **主机名纪律**：`source_url` / `download_url` MUST 为**相对路径**，不得写死主机名或本地文件路径（防止把 `C:\...` 之类写进公开产物）。

### 4.2 `GET /api/knowledge/sources/<chunk_id>`

**读取来源时重新鉴权并核对正文版本**：

1. `login_required`（无会话 → 401）
2. 从 `session["class_id"]` 取班级；`WHERE c.id = ? AND c.class_id = ?`（双条件）
3. 命中不到 → **404，与"不存在"完全同形同体**
4. 核对版本：`chunks.source_sha256` 与当前 `materials.source_sha256` 比较
   - 一致 → `200`，返回块正文、`char_*` / `line_*`、`chunk_sha256`、`download_url`
   - 不一致 → **`409 source_stale`**，**不返回正文**，提示"来源已过期，请重新检索"

### 4.3 状态码约定（全项目统一）

| 情形 | 状态码 | 响应体 |
| --- | --- | --- |
| 无会话 | `401 unauthorized` | `{"error":"unauthorized",...}` |
| 会话角色非 `teacher`/`student` | `403 forbidden` | 含 `required_role`、`your_role` |
| `q` 缺失/空/超长/含控制字符、`limit` 越界 | `400 invalid_query` | 含具体违反项 |
| 来源 chunk 跨班 **或** 不存在 | `404 not_found` | **两者字节级一致** |
| 来源版本过期 | `409 source_stale` | 不含正文 |
| 数据库故障 | `503` | 不含任何材料内容 |
| 成功 | `200` | 见 4.1 |

## 5. 索引生命周期

| 阶段 | 触发 | 行为 |
| --- | --- | --- |
| **建** | 教师上传成功 | `BEGIN` → 写 `materials` + `knowledge_entries` + `knowledge_chunks` + 置 `index_status='indexed'`、`indexed_at`、`source_sha256` → `COMMIT`。任一步失败 → 整体回滚 + 删除落盘文件（沿用上一变更「失败无脏数据」契约） |
| **回填** | `scripts/backfill_index.py` | 幂等：对每篇材料计算 `sha256(正文)`；若 `materials.source_sha256` 已等于它且 `index_status='indexed'` → **跳过**；否则删旧块、重建、更新三列。可重复执行，结果不变 |
| **检测** | 每次检索 | 比对 `materials.source_sha256` 与 `chunks.source_sha256`；不一致或 `index_status != 'indexed'` → 计入 `index_warnings`，`degraded=true` |
| **重建** | 手动（`backfill_index.py --force`） | 全量重建，用于算法升级后统一坐标系 |
| **持久化** | 容器重启 / `down`+`up` | 索引在 SQLite 中，随 `./data` 卷持久；**无需重启后重建** |
| **删除** | 材料被删（当前无删除接口） | `knowledge_chunks` 走 `ON DELETE CASCADE` 同步清理 |

## 6. 溯源定位与"不生成虚构引用"

- 结果为**原文子串**，由 `str` 切片直接产生（`body_text[match : match + len(q)]` 的邻域），**不经过任何模型改写**。
- 本变更**不提供答案生成接口**（Non-goals），因此不存在"引用与原文不符"或"编造引用"的可能 —— 引用即原文切片 + 精确坐标 + 内容哈希。
- 每个结果都能被独立复核：`source_url` 重新读取时**再次鉴权**并**核对版本**；`source_sha256` 若与当前正文不符则明确返回 `409`，**绝不返回一个与当前正文不一致的片段当作有效引用**。

## 7. 安全边界

1. **授权只来自会话**：`class_id`、`role` 只取 `session`；`query` / `body` / `header` 中的 `class_id`、`class`、`role`、`user_id` 一律**忽略**。
2. **班级过滤在最前**：见 §3.3；并有验收用例通过"移除谓词即失败"反向证明其必要性。
3. **禁止检出后过滤**：禁止全库检索后隐藏，禁止先全库排序再筛班级。
4. **不泄露他班**：结果、`total_matched`、`index_warnings`、错误信息均不含他班标题、正文、路径、计数。
5. **404 同形**：跨班来源与不存在来源**字节级相同**，不通过状态码或响应体差异枚举他班材料存在性（沿用上一变更固定 404 的取舍）。
6. **溯源再审**：`sources` 端点不复用检索时的判定结果，独立走鉴权 + 双条件 + 版本核对。
7. **前端转义**：查询词回显、`title`、`snippet` 一律转义后写入 DOM（`textContent` 或模板自动转义）；**禁止** `innerHTML`/`v-html` 拼接。片段可含 `<script>` 等上传内容，必须按文本渲染。
8. **会话失效清理**：收到 401/403 时前端 MUST 清空上一次的 `results`、`total_matched` 与查询回显，避免旧结果残留误导。
9. **输入即数据**：`q` 只作参数化查询的值，绝不拼接进 SQL 字符串；也不作为正则求值。

## 8. 向量数据库（本变更不采用，但保留扩展点）

**本变更不采用向量数据库。** 理由：本变更要交付的是「**可复现**的关键词检索 + 溯源」，语义召回属另一类能力（Non-goals 已排除）。引入向量库会把模型选型、维度、嵌入服务可用性、增量同步、以及"同一查询在不同模型版本下结果不同"的**不可复现性**带进验收面。

若后续变更为采用，MUST 补齐以下四项（写入彼时 design.md 后方可实施）：

| 项 | 要求 |
| --- | --- |
| **模型** | 明确名称与版本，如 `bge-small-zh-v1.5`（中文）/ `text-embedding-3-small`；须说明离线可用性与降级策略 |
| **维度** | 明确向量维度（如 `512` / `1536`），并与存储 schema 绑定 |
| **元数据班级过滤** | 向量记录 MUST 携带 `class_id` 元数据，且班级过滤 MUST 作为 **ANN 检索的过滤参数**（pre-filter），**禁止**检索后过滤 |
| **同步机制** | 上传时同步写向量；正文变更时重建该材料向量；删除材料时级联删向量；失败可重试且**幂等**（以 `chunk_sha256` 为幂等键） |

**方案对比（供后续决策）**

| 方案 | 中文支持 | 新依赖 | 班级过滤位置 | 可复现性 |
| --- | --- | --- | --- | --- |
| **SQLite 字面匹配（本变更）** | 无需分词，直接子串 | 无 | SQL `WHERE`（最前，单表） | **完全确定** |
| SQLite FTS5 + `trigram` | 支持，但 tokenizer 依赖 SQLite 构建选项 | 无（若可用） | SQL `WHERE` / FTS 查询 | 依赖构建，需探测可用性 |
| 向量库 | 语义召回 | 模型 + 向量存储 | ANN pre-filter | 受模型版本影响 |

## 9. 迁移与回退

**迁移（向前，幂等）**

1. `db.SCHEMA_SQL` 追加 `knowledge_chunks` 建表 + 索引（`CREATE TABLE/INDEX IF NOT EXISTS`）。
2. `materials` 三列用 `ALTER TABLE ... ADD COLUMN`，实施时先查 `PRAGMA table_info(materials)`，**列已存在则跳过**（可重复执行）。
3. 既有行不受影响：新列可为 `NULL`（`index_status` 默认 `'pending'`）。
4. 迁移后执行 `scripts/backfill_index.py` 为历史材料建索引；未回填前检索返回 `degraded:true`，**不返回假"无命中"**。

**回退（向后，低风险）**

- `knowledge_chunks` 与三个新列是**派生数据**，可由 `materials` + `knowledge_entries` **完整重建**。
- 回退只需：`DROP TABLE knowledge_chunks;` 并忽略 `materials` 的三个新列（保留即可，不影响既有查询）。
- **不需回退**（不被本变更触碰）：`classes`、`users`、`materials` 既有列语义、`knowledge_entries`、上传/下载/登录/会话、Docker 启动方式、`class_id` 语义。
- 回退后既有 23 + 58 项验收 MUST 仍全绿（本变更为**增量**，不修改既有行为）。

## 10. 风险与取舍

| 风险 / 取舍 | 影响 | 处理 |
| --- | --- | --- |
| **跨块边界的关键词不命中** | 长词跨段时查不到 | 已明确为**已知行为**并写入 spec 场景；可通过调大块上限缓解 |
| **无分词 / 无同义词 / 无拼音** | 中文需连续子串；「方程解法」查不到「解方程」 | Non-goals 显式声明；属"可复现"的代价 |
| **全角与半角不互通** | 全角 `Ａ` 不匹配半角 `A` | 已知取舍；未来可加 NFKC 匹配层（需引入偏移映射表） |
| **大材料导致块扫描** | 班级级数据量（≤10⁴ 块）可接受；万级材料后变慢 | 接口不变，后续可换 FTS5（D1 备选） |
| **`hit_count` 字段** | 若作为持久列会随查询变化，引入写放大 | 实施时**优先不落库**，在查询中即时计算；该列仅为预留，可为 `0` |
| **改写既有上传事务** | 触碰已验收代码有回归风险 | 上传路径仅**追加**写块逻辑，既有断言（行数、class 一致性、无脏数据）MUST 保持全绿 |
| **无 openspec CLI 时的校验** | 本地无 `openspec` 命令 | 用仓库内 `scripts/validate_spec.py` 作**等价校验**并如实记录；有 CLI 的环境应补跑 `openspec validate add-class-scoped-knowledge-search --strict` |
