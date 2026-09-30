# traceable-vector-retrieval 能力规约（delta）

> 本文件是 `add-traceable-vector-retrieval` 的增量规约（delta）。来源：第 4 课课件
> 《登录认证与知识库问答》（2026-09-30 发布）要求摘录 + `/week04/` 教学原型实测接口契约。
> 每条需求使用 Requirement + Scenario 结构，Scenario 以 WHEN / THEN / AND 书写，
> **唯一标准是能否直接判定通过与否**。负路径与正路径同等重要（AGENTS.md 第二条）。
>
> 术语：`class_id` / `role` 一律指**会话中的服务端值**；「码点」指 Unicode code point；
> 「预处理后文本」指材料正文经换行归一化 → 可选清洗 → NFKC 之外的 NFC 归一化后的整篇文本；
> 「切片」= `knowledge_chunks` 的一行 = 一个 Qdrant point。

## ADDED Requirements

### Requirement: 班级限定的检索范围

系统 MUST 在**两条检索路径的候选生成之前**限定为会话所属班级：关系库路径的 `class_id` MUST 是 SQL 的第一条谓词；向量库路径 MUST 以 `class_id` 作为 ANN 检索的 **pre-filter**；回表取正文 MUST 再次带 `class_id` 条件。客户端通过 query、body 或 header 传入的 `class_id`、`class`、`role`、`user_id` MUST 被**丢弃**，不参与授权。系统 MUST NOT 采用「全库检索后再在前端或服务端隐藏他班结果」的实现方式。

#### Scenario: 教师检索仅返回本班命中
- **WHEN** 教师 A 以本班材料中确实存在的关键词调用 `POST /api/search`
- **THEN** 每条 `hits[]` 的 `material_id` 所归属的班级均为 A 班
- **AND** 响应中不含任何 B 班标题、正文片段或文件路径

#### Scenario: 学生与教师检索结果一致
- **WHEN** 学生 A1 与教师 A 以同一关键词、同一模式检索
- **THEN** 两者的 `hits[]` 集合与顺序一致
- **AND** 不出现任何 B 班内容

#### Scenario: 三种模式都执行班级过滤
- **WHEN** 分别以 `keyword`、`vector`、`hybrid` 三种模式检索同一 B 班独有词
- **THEN** 三种模式均返回 `200` 且 `hits` 为空数组（或 `status` 为 `no_evidence`）
- **AND** **MUST NOT** 返回 403 或 404，也不得出现 B 班任何材料标识

#### Scenario: 向量路径单独移除班级过滤即泄露（反向判据）
- **WHEN** 仅移除 Qdrant 查询中的 `class_id` filter，但保留回表时的 `class_id` 条件，重跑跨班用例
- **THEN** 跨班用例**仍不泄露**（证明回表条件构成第二层防线）
- **AND** 当**两处** `class_id` 同时移除时，跨班用例 MUST 失败（证明两层都在承担隔离职责）

#### Scenario: 关键字路径移除班级谓词即失败（反向判据）
- **WHEN** 将关键字检索 SQL 中的 `class_id` 谓词移除后重跑本组用例
- **THEN** 「跨班同关键词不泄露他班」用例失败
- **AND** 该反证用于判定班级过滤确实位于候选生成之前，而非事后过滤

#### Scenario: 客户端篡改班级或角色无效
- **WHEN** A 班用户在 query 串、JSON 正文或请求头中额外传入 `class_id=2`、`class=B`、`role=admin`
- **THEN** 服务端丢弃这些字段，检索范围仍为会话所属班级
- **AND** 响应与不携带这些字段时**完全一致**

#### Scenario: 无会话检索被拒绝
- **WHEN** 不携带有效会话调用 `POST /api/search` 或 `POST /api/ask`
- **THEN** 返回 HTTP 401
- **AND** 响应体不含任何材料标题、正文片段、文件名、路径或计数

#### Scenario: 跨班检索对外表现为空结果
- **WHEN** A 班会话检索一个仅存在于 B 班正文的词
- **THEN** 返回 HTTP 200，`hits` 为空数组
- **AND** 响应中不含可用于推断 B 班材料存在性的字段（不用 403/404）

### Requirement: 三种检索模式

系统 MUST 提供 `keyword`、`vector`、`hybrid` 三种模式，且三种模式的**数据访问面 MUST 明确且互不牵连**：`keyword` MUST NOT 调用嵌入接口、MUST NOT 访问向量库。

#### Scenario: keyword 模式不触达网关与向量库
- **WHEN** 以 `mode=keyword` 检索，且嵌入网关与 Qdrant 均不可用
- **THEN** 仍返回 HTTP 200 与正确的命中结果
- **AND** 该次请求未产生任何对嵌入接口或 Qdrant 的网络调用

#### Scenario: vector 模式走嵌入与向量库
- **WHEN** 以 `mode=vector` 检索
- **THEN** 响应含 `query_vector`，其长度等于 `EMBEDDING_DIM`
- **AND** 每条 `hits[]` 含非空 `score_vector` 与 `rank_vector`，`score_keyword` 与 `rank_keyword` 为 `null`

#### Scenario: hybrid 模式两路均参与
- **WHEN** 以 `mode=hybrid` 检索同一个在两路都可命中的词
- **THEN** 命中的条目 `score_keyword`、`score_vector`、`rank_keyword`、`rank_vector` 均非空
- **AND** `score_final` 等于 `1/(60+rank_keyword) + 1/(60+rank_vector)`

#### Scenario: 模式参数非法被拒绝
- **WHEN** `mode` 取 `keyword` / `vector` / `hybrid` 之外的值
- **THEN** 返回 HTTP 400，错误码为 `invalid_query`
- **AND** 不静默回退到默认模式后返回结果

### Requirement: 检索输入校验与字面匹配规则

系统 MUST 明确并强制检索输入的长度、字符与上限规则，MUST 采用**可复现的字面匹配**语义，MUST NOT 静默改写或截断用户输入。

#### Scenario: 空查询被拒绝
- **WHEN** 调用 `/api/search` 或 `/api/ask` 时 `q` 缺失、为空串或仅含空白字符
- **THEN** 返回 HTTP 400，错误码为 `invalid_query`
- **AND** 不返回任何结果集，且不触发全量扫描

#### Scenario: 超长查询被拒绝
- **WHEN** `q` 在 NFC 归一化后超过 300 个 Unicode 码点
- **THEN** 返回 HTTP 400，错误码为 `invalid_query`
- **AND** 不静默截断为前若干码点后再检索

#### Scenario: limit 与 offset 越界被拒绝
- **WHEN** `limit` 为 0、负数、非整数或大于 20；或 `offset` 为负数或大于 1000
- **THEN** 返回 HTTP 400，错误码为 `invalid_query`
- **AND** 不静默改用默认值

#### Scenario: 查询含控制字符被拒绝
- **WHEN** `q` 含 C0 或 C1 控制字符（如 `\x00`、`\x1f`、`\x7f`）
- **THEN** 返回 HTTP 400，错误码为 `invalid_query`
- **AND** 该请求不产生任何数据库查询

#### Scenario: 中文关键词按字面命中
- **WHEN** 以本班材料中存在的连续中文子串（如「一元二次方程」）检索
- **THEN** 返回至少一条结果，且其 `snippet` 含该连续子串
- **AND** 匹配不依赖分词结果，同一输入重复检索返回相同结果集与相同顺序

#### Scenario: ASCII 大小写不敏感
- **WHEN** 材料正文含 `Unit 1`，用户以 `unit 1` 检索
- **THEN** 命中该切片
- **AND** 返回的 `start_offset` / `end_offset` 与原文坐标一致（大小写折叠不改变长度）

#### Scenario: SQL 通配符按字面处理
- **WHEN** `q` 为 `%`、`_` 或 `\` 等 SQL 通配符字符
- **THEN** 仅按字面匹配含该字符的切片
- **AND** 不出现「`%` 匹配任意字符」而返回全部切片的行为

#### Scenario: 查询不被当作正则求值
- **WHEN** `q` 为 `.*`、`(a+)+$` 等正则元字符组合
- **THEN** 仅按字面匹配，不按正则语义求值
- **AND** 响应时间不因该输入显著增长（无 ReDoS）

### Requirement: 混合检索的融合公式与阈值

`hybrid` 模式 MUST 使用 RRF（k = 60）融合，MUST NOT 将两路原始分数相加；余弦相似度低于 0.35 的候选 MUST 被丢弃且 MUST NOT 进入向量结果（含 hybrid 的向量一路）。

#### Scenario: 两路命中的融合分
- **WHEN** 某切片在关键字路径与向量路径的名次分别为 `r_kw` 与 `r_vec`
- **THEN** `score_final = 1/(60+r_kw) + 1/(60+r_vec)`
- **AND** 该值不等于 `score_keyword + score_vector`（不是分数相加）

#### Scenario: 单路命中的融合分
- **WHEN** 某切片只在其中一路命中，另一路未召回
- **THEN** `score_final = 1/(60+rank)`，缺席的一路不贡献分数
- **AND** 未命中路径的 `score_*` 与 `rank_*` 字段为 `null`

#### Scenario: 低于阈值的候选被丢弃
- **WHEN** 某个切片的余弦相似度低于 0.35
- **THEN** 该切片不出现在 `vector` 模式的 `hits` 中
- **AND** 在 `hybrid` 模式下该切片也不进入向量结果，因此其 `score_final` 中不含向量项

#### Scenario: 阈值边界可判定
- **WHEN** 某切片余弦相似度恰为 0.35
- **THEN** 该切片被保留（阈值为下界，含等号）
- **AND** 该行为在 `vector` 与 `hybrid` 两种模式下一致

#### Scenario: 排序稳定可复现
- **WHEN** 多条切片的 `score_final` 相同
- **THEN** 按 `material_id` 升序、`chunk_index` 升序排列
- **AND** 重复执行同一检索返回完全相同的顺序

### Requirement: 结果字段与溯源信息完整性

每条检索结果 MUST 提供真实原文片段与可回溯的定位信息，并 MUST 注明 Unicode 坐标定义与偏移基准。

#### Scenario: 每条 hit 字段完整
- **WHEN** 检索返回至少一条结果
- **THEN** 每条 hit 均含 `chunk_id`、`material_id`、`material_title`、`chunk_index`、`start_offset`、`end_offset`、`snippet`、`score_keyword`、`score_vector`、`rank_keyword`、`rank_vector`、`score_final`
- **AND** 任一条目缺少上述字段即判定不通过

#### Scenario: 偏移坐标可复核
- **WHEN** 取某条 hit 的 `start_offset` 与 `end_offset`，在**预处理后文本**上按码点切片
- **THEN** 切出的文本与 `snippet` 一致
- **AND** 响应明确声明坐标单位为 Unicode 码点、`offset_basis` 为预处理后文本

#### Scenario: 摘要对象为切片正文
- **WHEN** 查看任一条 hit 的 `snippet`
- **THEN** `snippet` 是该切片 `body_text` 的子串，不是材料全文、也不是 `knowledge_entries` 整行
- **AND** 摘录**取自关系库**，不从向量库取正文

#### Scenario: 行号在 CRLF 与 LF 下一致
- **WHEN** 同一段正文分别以 `\r\n` 与 `\n` 结尾存储后检索同一关键词
- **THEN** 两次返回的 `line_start` / `line_end` 相同（兼容别名端点提供行号字段）
- **AND** 行号为 LF 归一化后的 1 起行号

#### Scenario: 向量分量不进入页面
- **WHEN** 浏览器加载检索页面并执行一次 `mode=vector` 的检索
- **THEN** 页面**不展示**任何向量分量
- **AND** 响应中的 `query_vector` 仅供接口层使用，不渲染到 DOM

### Requirement: 来源读取重新鉴权与版本核对

来源端点 MUST 独立重新鉴权，MUST 核对来源版本与当前正文是否一致，MUST NOT 返回与当前正文不一致的片段当作有效引用。

#### Scenario: 跨班来源与不存在来源同形
- **WHEN** A 班用户分别以 B 班某切片的 `chunk_id` 与一个不存在的 `chunk_id` 访问来源端点
- **THEN** 两次均返回 HTTP 404
- **AND** 两次响应体**字节级一致**，无法区分「跨班存在」与「不存在」

#### Scenario: 来源过期时明确失败
- **WHEN** 某切片入库后材料正文被更新，导致该切片版本与当前正文不一致，用户再访问其来源
- **THEN** 返回 HTTP 409，错误码为 `source_stale`
- **AND** 响应不返回正文内容，并提示来源已过期需重新检索

#### Scenario: 来源读取会话失效
- **WHEN** 无有效会话时访问来源端点
- **THEN** 返回 HTTP 401
- **AND** 响应体不含任何切片正文、材料标题或路径

#### Scenario: 引用真实可回溯
- **WHEN** 以 hit 的 `chunk_id` 访问来源端点
- **THEN** 返回的切片正文与检索时给出的 `snippet` 所在切片一致
- **AND** 返回的 `chunk_sha256` 与检索结果中的一致，可用于核对片段未被篡改

### Requirement: 切分策略与预处理

系统 MUST 支持 `auto`、`custom`、`hierarchy` 三种切分策略，MUST 明确预处理规则，且预处理 MUST NOT 改写原文。

#### Scenario: auto 为默认策略且按优先边界断开
- **WHEN** 上传材料时未指定切分参数
- **THEN** 生效策略为 `auto`，最大长度 800 字、重叠 10%
- **AND** 切片边界优先落在空行、其次换行、再次句号，最后才按窗口硬切

#### Scenario: custom 参数越界被拒绝
- **WHEN** `/api/materials/:id/reindex` 的 `chunk_max_chars` 小于 100 或大于 2000，或 `chunk_overlap_percent` 小于 0 或大于 50
- **THEN** 返回 HTTP 400，错误码为 `invalid_query`
- **AND** 不静默夹取到合法区间后执行重建

#### Scenario: hierarchy 按标题分章
- **WHEN** 以 `chunk_strategy=hierarchy` 重建一份含 `#` / `##` / `###` 标题的 Markdown 材料
- **THEN** 每个章节的切片不跨章节标题边界（单章过长时仅在本章内按窗口切）
- **AND** 每个切片记录其所属章节路径

#### Scenario: 预处理不改写原文
- **WHEN** 启用 `collapse_whitespace=true` 与 `strip_links=true` 重建材料后查看材料正文
- **THEN** `materials` / `knowledge_entries` 中的正文与上传原文**逐字节一致**
- **AND** 切片偏移是相对**预处理后文本**，且响应通过 `offset_basis` 声明这一点

#### Scenario: 切片确定可复现
- **WHEN** 对同一正文以同一参数连续执行两次重建
- **THEN** 两次得到完全相同的切片划分、`start_offset`/`end_offset` 与 `chunk_sha256`
- **AND** 切片总数不变

#### Scenario: 超长无空行正文被硬切
- **WHEN** 材料中存在不含空行、长度超过 `max_chars` 的连续正文
- **THEN** 该正文被切分为多个切片，每片长度不超过 `max_chars`
- **AND** 每片均记录相对预处理后文本的码点偏移与行号范围

#### Scenario: 跨切片边界的关键词不命中
- **WHEN** 某关键词的子串横跨两个相邻切片的边界，且任一片内均不含完整关键词
- **THEN** 该关键词不产生命中
- **AND** 该行为属已声明的确定性取舍，检索不因跨片拼接而产生无法定位的结果

#### Scenario: 全空白材料不产生可用索引
- **WHEN** 材料正文解析后为空或仅含空白
- **THEN** 该材料不产生任何可检索切片，材料 `index_status` 标记为 `failed`
- **AND** 其失败状态在检索响应中以降级告警体现，不导致检索整体失败

### Requirement: 索引生命周期与一致性

索引 MUST 与材料正文一致提交；历史材料 MUST 可幂等回填；索引缺失或过期 MUST NOT 伪装成「没有结果」；失败 MUST NOT 留下脏数据；索引 MUST 在重启后保持可用。

#### Scenario: 上传后立即可检索
- **WHEN** 教师上传合法 `.md` / `.txt` 且入库成功
- **THEN** 切片与 `materials`、`knowledge_entries` **同事务**写入，响应包含 `chunk_count`
- **AND** 随后立即以 `keyword` 模式检索其正文中的关键词即可命中该材料

#### Scenario: 解析失败整体回滚无脏数据
- **WHEN** 上传过程中解析或切片步骤失败
- **THEN** 整个事务回滚，`materials`、`knowledge_entries`、`knowledge_chunks` 均无残留记录
- **AND** 不保留孤立落盘文件，接口返回 4xx 或 5xx 而非成功

#### Scenario: 嵌入失败保留材料但不写不完整向量
- **WHEN** 切片已成功写入，但嵌入调用失败或返回维度不等于 `EMBEDDING_DIM`
- **THEN** **材料记录被保留**，对应切片 `index_status` 为 `failed`，**不写入不完整向量**
- **AND** 该材料的正文仍可通过 `keyword` 模式检索到

#### Scenario: 历史材料幂等回填
- **WHEN** 对已有材料执行回填脚本一次，再执行第二次
- **THEN** 第一次为历史材料建立切片与向量，检索可命中
- **AND** 第二次不产生重复切片、不重复嵌入，切片数与哈希保持不变（幂等）

#### Scenario: 索引缺失不伪装成无命中
- **WHEN** 本班存在 `index_status` 非 `indexed` 的材料，用户检索一个实际存在于这些材料中的关键词
- **THEN** 返回 HTTP 200 且 `degraded` 为 `true`，`index_warnings` 列出受影响材料的标识与状态
- **AND** 响应 MUST NOT 以 `status:"no_evidence"` 与空 `hits` 冒充「没有结果」

#### Scenario: 重启后索引仍然可用
- **WHEN** 应用重启（或容器 `down` 后 `up`，未删除数据卷）
- **THEN** 既有切片与向量仍存在，检索可命中
- **AND** 无需重新执行全量回填

### Requirement: 显式重建索引

换切分策略 MUST 显式重建；重建 MUST 先删旧切片与旧向量，再写入新的；重建 MUST 幂等且 MUST 同步清理向量库中的陈旧点。

#### Scenario: 重建先删后写
- **WHEN** 教师以新的切分参数调用 `POST /api/materials/:id/reindex`
- **THEN** 该材料的旧切片与**旧向量**先被删除，再写入新切片与新向量
- **AND** 重建后 `chunk_count` 与 `chunk_strategy` 反映新策略，向量库中该材料的点数等于新切片数

#### Scenario: 重建后检索结果同步更新
- **WHEN** 重建改变了切片边界
- **THEN** 后续检索返回的 `chunk_index` 与 `start_offset` / `end_offset` 基于新切片
- **AND** 旧的 `chunk_id` 不再出现在检索结果中

#### Scenario: 学生不得重建索引
- **WHEN** 学生会话调用 `POST /api/materials/:id/reindex`
- **THEN** 返回 HTTP 403，且响应含 `required_role` 与 `your_role`
- **AND** 该材料切片与向量均未发生变化

#### Scenario: 跨班重建被拒且同形
- **WHEN** A 班教师对 B 班材料调用重建接口
- **THEN** 返回 HTTP 404，与「材料不存在」同形同体
- **AND** 不暴露该材料是否存在于其他班级

### Requirement: 基于知识库的简短问答（先检索再生成）

`POST /api/ask` MUST 先由检索模块以 `hybrid` 模式取回**本班前 4 条**切片；**仅当存在命中切片时**才调用对话网关；MUST NOT 在无依据时把问题提交给生成模型。

#### Scenario: 有命中才调用生成模型
- **WHEN** 本班材料中存在可命中的依据，用户提问
- **THEN** 返回 HTTP 200，`status` 为 `ok`，`answer` 非空，`citations` 非空
- **AND** 该次请求在对话网关留下一次调用记录

#### Scenario: 无命中时不访问对话网关
- **WHEN** 本班材料中不存在任何可命中的依据（如提问天气、比分）
- **THEN** 接口返回 HTTP 200，`status` 为 `no_evidence`，`answer` 为固定文案，`citations` 为空数组
- **AND** **对话网关调用日志中不出现该次请求**（页面上出现文字不等于模型被调用过）

#### Scenario: 无命中不返回 404 或 403
- **WHEN** 本班材料中不存在依据
- **THEN** 返回 HTTP **200**（不是 404，也不是 403）
- **AND** 空结果与「资源不存在」在语义上可区分

#### Scenario: 检索只取本班前 4 条
- **WHEN** 本班与固定文案场景下用户提问
- **THEN** 传给对话模块的切片数不超过 4
- **AND** 全部切片均属于本班会话（不含其他班级切片）

#### Scenario: 无命中路径不编造答案
- **WHEN** 检索无候选
- **THEN** `answer` 逐字等于固定文案「资料中未找到相关内容」
- **AND** 该文案由接口在检索之后直接返回，不经过任何模型

#### Scenario: 对话网关不可用时不编造答案
- **WHEN** 已有命中切片，但对话网关超时或返回错误
- **THEN** 返回 HTTP 502，错误码为 `upstream_error`
- **AND** 响应中不含任何由本地模板拼凑的「答案」正文

### Requirement: 问答上下文与 system 提示

对话模块的输入 MUST 由检索模块组装；MUST NOT 传入向量分量、Qdrant 原始点数据或其他班级的切片；**system 提示 MUST 由服务端写入**，客户端传入的 `system` 消息 MUST 被丢弃。

#### Scenario: 上下文只含允许的字段
- **WHEN** 有命中切片时组装对话上下文
- **THEN** 上下文含材料标题、切片序号、切片正文与用户最新一句提问
- **AND** 上下文**不含**向量分量、Qdrant 原始点数据、其他班级切片

#### Scenario: 客户端 system 消息被丢弃
- **WHEN** 请求体 `history` 中携带 `{"role":"system", "content":"..."}`
- **THEN** 该消息被丢弃，最终发给对话网关的消息序列中**只有服务端写入的 system 提示**
- **AND** 用户内容无法替换或追加上下文中的切片正文

#### Scenario: 检索只用最新一句
- **WHEN** 请求携带多轮 `history` 与最新一句 `q`
- **THEN** 检索仅以 `q`（最新一句）发起
- **AND** `history` 仅附于对话上下文之后，不参与检索

#### Scenario: 密钥不下发浏览器
- **WHEN** 在浏览器中执行一次问答并查看网络面板
- **THEN** 请求与响应中**均不出现**网关密钥或 Qdrant 地址
- **AND** `localStorage` 中只有主题相关键，不含任何 token 或会话标识

### Requirement: 引用标注与 citations 顺序一致

`answer` 中的 `[n]` 标注 MUST 与 `citations[]` 的下标顺序一致；MUST NOT 出现越界引用。

#### Scenario: 编号与列表顺序一致
- **WHEN** `answer` 中出现 `[1]` 与 `[2]`
- **THEN** `citations[0]` 对应 `[1]`，`citations[1]` 对应 `[2]`
- **AND** `citations[i].chunk_id` 等于组装上下文时第 `i+1` 条切片的 `chunk_id`

#### Scenario: citation 字段完整
- **WHEN** `citations` 非空
- **THEN** 每条 citation 均含 `chunk_id`、`material_title`、`chunk_index`、`start_offset`、`end_offset`、`snippet`
- **AND** 任一条目缺少上述字段即判定不通过

#### Scenario: 越界引用被剔除
- **WHEN** 模型输出引用了超出实际传入切片数的编号
- **THEN** 该编号对应的引用不出现在 `citations` 中
- **AND** 服务端记录一条 warning，且不返回任何他班或不存在切片的引用

#### Scenario: 引用可回溯到原文
- **WHEN** 以 `citations[i].chunk_id` 访问来源端点
- **THEN** 返回的切片正文与 `citations[i].snippet` 所在切片一致
- **AND** 该切片属于提问者所在班级

### Requirement: 认证沿用服务端会话

第 4 课 MUST 沿用第 3 课的**服务端会话 Cookie** 认证，MUST NOT 引入客户端持有凭据的登录方案。会话中 MUST 存有该用户的 `class_id`，检索与问答模块 MUST 只从请求上下文读取班级。

#### Scenario: 会话 Cookie 即登录态
- **WHEN** 调用 `POST /api/login` 并成功后查看浏览器存储
- **THEN** 登录态由 `HttpOnly` 会话 Cookie 承载
- **AND** 响应体与 `localStorage` 中不出现用于后续鉴权的 bearer token

#### Scenario: 请求中携带的班级一律丢弃
- **WHEN** 在查询字符串、JSON 正文或请求头中分别携带 `class_id`
- **THEN** 三种位置的取值都被丢弃，结果与会话所属班级完全一致
- **AND** 与不携带这些字段时的响应**逐字段相同**

#### Scenario: 会话信息端点
- **WHEN** 已登录会话调用 `GET /api/me`
- **THEN** 返回该会话的 `user_id`、`role`、`class_id` 与班级名
- **AND** 这些值全部来自服务端会话，不接受客户端输入覆盖

#### Scenario: 登出使会话失效
- **WHEN** 调用 `POST /api/logout` 后再次调用 `/api/search`
- **THEN** 返回 HTTP 401
- **AND** 前端据此清理状态并回到登录页

### Requirement: 页面安全与状态区分

检索与问答页面 MUST 对查询回显、材料标题与正文片段做输出转义；MUST 在会话失效时清理旧结果；MUST 让用户可区分空结果、降级与服务故障。

#### Scenario: 查询词与片段被转义
- **WHEN** 查询词或材料正文片段中含 HTML/脚本片段（如 `<script>`）
- **THEN** 页面以**纯文本**呈现该内容，不执行其中的脚本
- **AND** 页面不通过 `innerHTML` 等方式拼接未转义的查询词、标题或片段

#### Scenario: 会话失效时清理旧结果
- **WHEN** 页面在已有结果的状态下收到 401 或 403
- **THEN** 页面清空上一次的 `hits`、`citations`、计数与查询回显
- **AND** 提示需重新登录，不保留上一会话的结果

#### Scenario: 三种状态在页面上可区分
- **WHEN** 分别出现「无命中」、「索引降级」、「服务故障」三种情形
- **THEN** 页面给出三种不同的提示文案，不共用同一个「无结果」提示
- **AND** 降级情形下页面额外提示受影响的材料数量或标识

#### Scenario: 来源过期的页面提示
- **WHEN** 用户点击来源链接但该来源已过期（409）
- **THEN** 页面提示来源已过期并引导重新检索
- **AND** 页面不把过期内容当作有效正文展示

### Requirement: 接口契约一致性

本变更新增的端点 MUST 与课件实测契约在路径、请求字段、响应字段与状态码上一致；既有第 3 课端点的行为 MUST NOT 改变。

#### Scenario: 检索接口响应形状符合契约
- **WHEN** 调用 `POST /api/search`，请求体 `{q, mode, limit, offset}`
- **THEN** 响应含 `query_vector`、`hits[]`、`status`
- **AND** `status === "no_evidence"` 时响应附 `message`

#### Scenario: 问答接口响应形状符合契约
- **WHEN** 调用 `POST /api/ask`，请求体 `{q, history}`
- **THEN** 响应含 `answer`、`status`、`citations[]`
- **AND** citation 字段集与契约一致

#### Scenario: 上传返回切片摘要
- **WHEN** 以 `multipart/form-data` 上传含 `file` 与切分参数的文件
- **THEN** 成功响应的 JSON 含 `title`、`chunk_strategy`、`chunk_count`
- **AND** 学生上传仍返回 403、类型不支持仍返回 400（沿用上一变更）

#### Scenario: 材料列表分页
- **WHEN** 以 `page`、`page_size`、`title` 调用 `GET /api/materials`
- **THEN** 返回本班材料分页结果与总数字段
- **AND** 结果不含其他班级材料，未传分页参数时默认 `page=1`、`page_size=20`

#### Scenario: 教学对照端点
- **WHEN** 已登录用户调用 `GET /api/materials/:id/vectors`
- **THEN** 返回该材料的切片行与对应向量库记录的对照信息（含 `chunk_id`、`chunk_index`、`chunk_sha256`、向量是否就绪）
- **AND** 响应**不回传向量分量**，且跨班材料返回 404

### Requirement: 范围纪律

本变更 MUST NOT 引入流式长对话、跨班检索、作业、管理员、PDF/OCR 等超出范围的能力，MUST NOT 修改既有已验收行为。

#### Scenario: 无流式输出
- **WHEN** 调用 `POST /api/ask`
- **THEN** 响应为一次性 JSON，不是 `text/event-stream` 也不是分块流
- **AND** 仓库中不存在面向第 5 课的流式对话入口

#### Scenario: 无跨班检索入口
- **WHEN** 枚举本变更新增的全部路由
- **THEN** 不存在接受客户端指定班级、或可检索全部班级的端点
- **AND** 所有检索类端点的班级来源均为服务端会话

#### Scenario: 未引入新文件类型
- **WHEN** 尝试上传 PDF、图片或 Office 文档
- **THEN** 仍按上一变更的类型白名单返回 400
- **AND** 本变更未扩展可解析的文件类型，也未引入 OCR

#### Scenario: 既有行为无回归
- **WHEN** 执行上一变更的既有验收
- **THEN** 登录、会话、角色权限、班级隔离、上传入库、下载与 `/health` 的行为保持不变
- **AND** 既有验收项全部通过，新增能力不改变已有状态码约定（跨班材料详情仍为 404）

#### Scenario: 未归档前不进入长期真相源
- **WHEN** 本变更仍有未通过的验收项
- **THEN** 本变更的规格 MUST NOT 出现在 `openspec/specs/`（长期真相源）
- **AND** 仅在全部验收通过后，才由归档流程写入 `openspec/specs/`
