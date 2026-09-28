# class-knowledge-search 能力规约（delta）

> 本文件是 `add-class-scoped-knowledge-search` 的增量规约（delta），来源为第四课作业要求：
> **实现限定班级范围的知识库检索，支持内容溯源。**
> 每条需求使用 Requirement + Scenario 结构，Scenario 以 WHEN / THEN / AND 书写，
> **唯一标准是能否直接判定通过与否**。负路径与正路径同等重要（AGENTS.md 第二条）。
>
> 术语：`class_id` / `role` 一律指**会话中的服务端值**；「码点」指 Unicode code point；
> 「材料全文」指 `knowledge_entries.body_text` 经 LF 与 NFC 归一化后的整篇文本。

## ADDED Requirements

### Requirement: 班级限定的检索范围

系统 MUST 在**候选生成的第一条谓词**上限定为会话所属班级，并在**匹配、计数、排序与 top-k/limit 之前**完成班级过滤。客户端通过 query、body 或 header 传入的 `class_id`、`class`、`role`、`user_id` MUST NOT 参与授权。系统 MUST NOT 采用「全库检索后再在前端或服务端隐藏他班结果」的实现方式。

#### Scenario: 教师检索仅返回本班命中
- **WHEN** 教师 A 以本班材料中确实存在的关键词调用 `GET /api/knowledge/search`
- **THEN** 每条结果的 `material_id` 所归属的班级均为 A 班
- **AND** `total_matched` 等于 A 班匹配块数，与全库匹配总数无关

#### Scenario: 学生检索仅返回本班命中
- **WHEN** 学生 A1 以同一关键词检索
- **THEN** 返回结果与教师 A 的同关键词本班结果集合一致
- **AND** 不出现任何 B 班标题、正文片段或文件路径

#### Scenario: 无会话检索被拒绝
- **WHEN** 不携带有效会话调用 `GET /api/knowledge/search?q=<关键词>`
- **THEN** 返回 HTTP 401
- **AND** 响应体不含任何材料标题、正文片段、文件名、路径或计数

#### Scenario: 未知角色检索被拒绝
- **WHEN** 会话角色既非 `teacher` 也非 `student` 的会话调用检索
- **THEN** 返回 HTTP 403
- **AND** 响应体不含任何本班或他班的检索结果与计数

#### Scenario: 客户端篡改班级或角色无效
- **WHEN** A 班用户在 query 串或请求体中额外传入 `class_id=2`、`class=B`、`role=admin` 等字段
- **THEN** 服务端忽略这些字段，检索范围仍为会话所属班级
- **AND** 结果中不出现 B 班任何材料

#### Scenario: 跨班同关键词不泄露他班
- **WHEN** A 班与 B 班材料中存在**同一个关键词**，A 班用户检索该关键词
- **THEN** 结果只含 A 班块，且 `total_matched` 与 A 班匹配数一致
- **AND** 响应中出现该关键词的任何位置均不暴露 B 班块、B 班材料 ID 或 B 班块数

#### Scenario: 班级过滤承担隔离职责（反向判据）
- **WHEN** 将检索 SQL 中的 `class_id` 谓词移除后重跑本组用例
- **THEN** 「跨班同关键词不泄露他班」用例失败
- **AND** 该反证用于判定班级过滤确实位于候选生成之前，而非事后过滤

### Requirement: 检索输入与字面匹配规则

系统 MUST 明确并强制检索输入的长度、字符与上限规则，MUST 采用**可复现的字面匹配**语义，MUST NOT 静默改写或截断用户输入。

#### Scenario: 空查询被拒绝
- **WHEN** 调用检索时 `q` 缺失、为空串或仅含空白字符
- **THEN** 返回 HTTP 400，错误码为 `invalid_query`
- **AND** 不返回任何结果集，且不触发全量扫描

#### Scenario: 超长查询被拒绝
- **WHEN** `q` 在 NFC 归一化后超过 64 个 Unicode 码点
- **THEN** 返回 HTTP 400，错误码为 `invalid_query`
- **AND** 不返回结果，也不静默截断为前 64 个码点后再检索

#### Scenario: limit 越界被拒绝
- **WHEN** `limit` 为 0、负数、非整数或大于 20
- **THEN** 返回 HTTP 400，错误码为 `invalid_query`
- **AND** 不返回结果，也不静默改用默认值

#### Scenario: 查询含控制字符被拒绝
- **WHEN** `q` 含 C0 或 C1 控制字符（如 `\x00`、`\x1f`、`\x7f`）
- **THEN** 返回 HTTP 400，错误码为 `invalid_query`
- **AND** 该请求不产生任何数据库查询

#### Scenario: 中文关键词按字面命中
- **WHEN** 以本班材料中存在的连续中文子串（如「一元二次方程」）检索
- **THEN** 返回至少一条结果，且其 `snippet` 含该连续子串
- **AND** 匹配不依赖分词结果，同一输入在重复检索时返回相同结果集与相同顺序

#### Scenario: ASCII 大小写不敏感
- **WHEN** 材料正文含 `Unit 1`，用户以 `unit 1` 检索
- **THEN** 命中该块
- **AND** 返回的 `char_start` / `char_end` 与原文坐标一致（大小写折叠不改变长度）

#### Scenario: emoji 关键词精确命中
- **WHEN** 以材料正文中存在的 emoji 序列检索
- **THEN** 命中对应块，`snippet` 含该 emoji
- **AND** 偏移以码点计，emoji 不被拆分为半个代理对

#### Scenario: SQL 通配符按字面处理
- **WHEN** `q` 为 `%`、`_` 或 `\` 等 SQL 通配符字符
- **THEN** 仅按字面匹配含该字符的块
- **AND** 不出现"`%` 匹配任意字符"而返回全部块的行为

#### Scenario: 查询不被当作正则求值
- **WHEN** `q` 为 `.*`、`(a+)+$` 等正则元字符组合
- **THEN** 仅按字面匹配，不按正则语义求值
- **AND** 响应时间不因该输入显著增长（无 ReDoS）

#### Scenario: 无命中状态明确
- **WHEN** 以本班材料中不存在的关键词检索，且本班索引完好
- **THEN** 返回 HTTP 200，`hit` 为 `false`，`results` 为空数组，`total_matched` 为 0
- **AND** `degraded` 为 `false` 且 `index_warnings` 为空数组，以区别于降级状态

### Requirement: 结果排序、上限与计数

系统 MUST 采用确定且可复现的排序规则，MUST 在班级过滤之后施加排序与上限，MUST 只统计本班匹配数。

#### Scenario: 命中次数多者优先
- **WHEN** 两个本班块对同一关键词的命中次数不同
- **THEN** 命中次数多的块排在前面
- **AND** 排序在班级过滤之后进行，不因他班块的高命中数而改变本班次序

#### Scenario: 同分结果次序稳定
- **WHEN** 多个本班块命中次数相同
- **THEN** 按 `material_id` 升序、块序号 `ord` 升序排列
- **AND** 重复执行同一检索返回完全相同的顺序

#### Scenario: 结果上限生效
- **WHEN** 本班匹配块数超过 `limit`
- **THEN** `returned` 等于 `limit`，`results` 长度等于 `returned`
- **AND** `total_matched` 反映本班匹配总数，可大于 `returned`

#### Scenario: 计数不泄露他班
- **WHEN** A 班用户检索一个在两班都存在的关键词
- **THEN** `total_matched` 只计 A 班匹配块
- **AND** 响应中不含他班匹配数、他班总数或可用于推算他班规模的字段

### Requirement: 内容溯源信息完整性

每条检索结果 MUST 提供真实原文片段与可回溯的定位信息，包括材料标识、片段标识、正文摘要、字符偏移、源行号、鉴权来源链接与下载链接，并 MUST 注明 Unicode 坐标定义与摘要对象。

#### Scenario: 每条结果字段完整
- **WHEN** 检索返回至少一条结果
- **THEN** 每条结果均含 `chunk_id`、`material_id`、`title`、`snippet`、`char_start`、`char_end`、`line_start`、`line_end`、`snippet_char_start`、`source_url`、`download_url`
- **AND** 任一条目缺少上述字段即判定不通过

#### Scenario: 偏移坐标可复核
- **WHEN** 取某条结果的 `char_start` 与 `char_end`，在被检索材料全文（NFC 后）上按码点切片
- **THEN** 切出的文本包含该结果 `snippet` 所指的命中位置
- **AND** 坐标单位在响应中明确声明为 Unicode 码点、`offset_base` 为材料全文

#### Scenario: 行号在 CRLF 与 LF 下一致
- **WHEN** 同一段正文分别以 `\r\n` 与 `\n` 结尾存储后检索同一关键词
- **THEN** 两次返回的 `line_start` / `line_end` 相同
- **AND** 行号为 LF 归一化后的 1 起行号

#### Scenario: 摘要对象明确
- **WHEN** 查看任一条结果的 `snippet` 与响应中的 `coordinate_system`
- **THEN** 响应声明 `snippet_object` 为块正文（`knowledge_chunk.body_text`）
- **AND** `snippet` 确实是该块正文的子串，而非材料全文或 `knowledge_entries` 整行

#### Scenario: 链接为鉴权相对路径
- **WHEN** 查看 `source_url` 与 `download_url`
- **THEN** 两者均为以 `/` 开头的相对路径，分别指向来源端点与既有下载端点
- **AND** 两者均不写死主机名，也不含本地文件系统路径

#### Scenario: 引用真实可回溯
- **WHEN** 以结果中的 `source_url` 访问来源
- **THEN** 返回的块正文与检索时给出的 `snippet` 所在块一致
- **AND** 返回的 `chunk_sha256` 与检索结果中的一致，可用于核对片段未被篡改

### Requirement: 来源读取重新鉴权与版本核对

来源端点 MUST 独立重新鉴权，MUST 核对来源版本与当前正文是否一致，MUST NOT 返回与当前正文不一致的片段当作有效引用。

#### Scenario: 跨班来源被拒绝且同形
- **WHEN** A 班用户以 B 班某块的 `chunk_id` 访问来源端点
- **THEN** 返回 HTTP 404
- **AND** 响应体与「该 `chunk_id` 不存在」时**字节级一致**，不暴露该块是否存在

#### Scenario: 不存在来源与跨班来源同形
- **WHEN** A 班用户以一个不存在的 `chunk_id` 访问来源端点
- **THEN** 返回 HTTP 404，响应体形态与跨班情形完全相同
- **AND** 无法通过状态码或响应体差异区分"跨班存在"与"不存在"

#### Scenario: 来源过期时明确失败
- **WHEN** 某块入库后材料正文被更新，导致该块版本与当前正文不一致，用户再访问其来源
- **THEN** 返回 HTTP 409，错误码为 `source_stale`
- **AND** 响应不返回正文内容，并提示来源已过期需重新检索

#### Scenario: 来源读取会话失效
- **WHEN** 会话已失效（无有效会话）时访问来源端点
- **THEN** 返回 HTTP 401
- **AND** 响应体不含任何块正文、材料标题或路径

#### Scenario: 索引缺失时来源仍要求鉴权
- **WHEN** 检索处于降级（索引缺失或过期）状态时访问来源端点
- **THEN** 仍先执行会话鉴权与班级双条件校验，再处理版本核对
- **AND** 不因降级状态而放宽鉴权或返回他班内容

### Requirement: 索引生命周期与一致性

知识库索引 MUST 与材料正文一致提交；历史材料 MUST 可幂等回填；索引缺失或过期 MUST NOT 伪装成「没有结果」；失败 MUST NOT 留下脏数据；索引 MUST 在重启后保持可用。

#### Scenario: 上传后立即可检索
- **WHEN** 教师上传合法 `.md` / `.txt` 且入库成功
- **THEN** 该材料的索引与 `materials`、`knowledge_entries` **同事务**写入
- **AND** 随后立即检索其正文中的关键词即可命中该材料

#### Scenario: 索引与正文一致提交
- **WHEN** 上传过程中建索引步骤失败
- **THEN** 整个事务回滚，`materials`、`knowledge_entries`、`knowledge_chunks` 均无残留记录
- **AND** 不保留孤立落盘文件，接口返回 4xx 或 5xx 而非成功

#### Scenario: 历史材料幂等回填
- **WHEN** 对已有材料执行回填脚本一次，再执行第二次
- **THEN** 第一次为本班历史材料建立索引，检索可命中
- **AND** 第二次不产生重复块，块数与正文哈希保持不变（幂等）

#### Scenario: 回填后索引状态正确
- **WHEN** 回填完成后查看材料索引状态
- **THEN** 已成功建索引的材料状态为已索引
- **AND** 正文为空或无法解析的材料状态为失败，且计入降级告警而非静默忽略

#### Scenario: 索引缺失不伪装成无命中
- **WHEN** 本班存在索引状态为待建、过期或失败的材料，用户检索一个实际存在于这些材料中的关键词
- **THEN** 返回 HTTP 200 且 `degraded` 为 `true`，`index_warnings` 列出受影响材料的标识与状态
- **AND** 响应 MUST NOT 以 `hit:false` 与空 `results` 冒充"没有结果"

#### Scenario: 索引过期被检出
- **WHEN** 材料正文已变更为新版本，但该材料的索引仍是旧版本
- **THEN** 检索响应该材料计入 `index_warnings`，`degraded` 为 `true`
- **AND** 不返回基于旧正文的片段作为有效命中

#### Scenario: 重启后索引仍然可用
- **WHEN** 应用重启（或容器 `down` 后 `up`，未删除数据卷）
- **THEN** 既有材料的索引仍存在，检索可命中
- **AND** 无需重新执行全量回填

#### Scenario: 数据库故障不与空结果混淆
- **WHEN** 检索过程中数据库不可读
- **THEN** 返回 HTTP 503
- **AND** 响应体不含任何材料标题、正文或路径，且前端可据此区分"故障"与"无命中"

### Requirement: 分块与文本边界

系统 MUST 明确定义分块算法与文本归一化规则，覆盖中文、emoji、CRLF 与特殊字符，并 MUST 明确跨块边界与图形簇边界的已知行为。

#### Scenario: 跨块边界的关键词不命中
- **WHEN** 某关键词的子串横跨两个相邻块的边界，且任一单块内均不含完整关键词
- **THEN** 该关键词不产生命中
- **AND** 该行为属已声明的确定性取舍，检索不因跨块拼接而产生无法定位的结果

#### Scenario: 超长段落被硬切
- **WHEN** 材料中存在不含空行、长度超过块上限的连续正文
- **THEN** 该正文被切分为多个块，每块长度不超过块上限
- **AND** 每块均记录相对材料全文的码点偏移与行号范围

#### Scenario: 空行作为段落边界
- **WHEN** 材料正文以空行分隔若干段落
- **THEN** 分块边界与段落边界对应，块数与段落分组关系稳定
- **AND** 同一材料重复建索引得到相同的块划分（确定性）

#### Scenario: emoji 与组合字符不破坏坐标
- **WHEN** 材料正文含 emoji 或带修饰符的组合字符，检索命中其后方的子串
- **THEN** 返回的码点偏移与行号仍能在材料全文上正确切出该子串
- **AND** 分块在码点边界进行，不产生半个代理对或不完整码点

#### Scenario: 全空白材料不产生可用索引
- **WHEN** 材料正文解析后为空或仅含空白
- **THEN** 该材料不产生任何可检索块，索引状态标记为失败
- **AND** 其失败状态在检索响应中以降级告警体现，不导致检索整体失败

### Requirement: 页面安全与状态区分

检索页面 MUST 对查询回显、材料标题与正文片段做输出转义；MUST 在会话失效时清理旧结果；MUST 让用户可区分空结果、故障与来源过期。

#### Scenario: 查询词与片段被转义
- **WHEN** 查询词或材料正文片段中含 HTML/脚本片段（如 `<script>`）
- **THEN** 页面以**纯文本**呈现该内容，不执行其中的脚本
- **AND** 页面不通过 `innerHTML` 等方式拼接未转义的查询词、标题或片段

#### Scenario: 会话失效时清理旧结果
- **WHEN** 页面在已有检索结果的状态下收到 401 或 403
- **THEN** 页面清空上一次的结果列表、计数与查询回显
- **AND** 不保留上一会话的结果以免误导，并提示需重新登录

#### Scenario: 三种状态在页面上可区分
- **WHEN** 分别出现"无命中"、"索引降级（缺失或过期）"、"服务故障"三种情形
- **THEN** 页面给出三种不同的提示文案，不共用同一个"无结果"提示
- **AND** 降级情形下页面额外提示受影响的材料数量或标识

#### Scenario: 来源过期的页面提示
- **WHEN** 用户点击来源链接但该来源已过期（409）
- **THEN** 页面提示来源已过期并引导重新检索
- **AND** 页面不把过期内容当作有效正文展示

### Requirement: 范围纪律

本变更 MUST NOT 引入聊天、问答生成、作业、管理员、跨班检索、PDF/OCR 等超出范围的能力，MUST NOT 修改既有已验收行为。

#### Scenario: 无跨班检索入口
- **WHEN** 枚举本变更新增的全部路由
- **THEN** 不存在接受客户端指定班级、或可检索全部班级的端点
- **AND** 所有检索类端点的班级来源均为服务端会话

#### Scenario: 无问答或摘要生成
- **WHEN** 调用本变更提供的能力
- **THEN** 只返回原文片段与定位信息，不返回由模型生成的答案、摘要或推测
- **AND** 仓库中不存在针对检索结果的大模型生成接口

#### Scenario: 未引入新文件类型
- **WHEN** 尝试上传 PDF、图片或 Office 文档
- **THEN** 仍按上一变更的类型白名单返回 400
- **AND** 本变更未扩展可解析的文件类型，也未引入 OCR

#### Scenario: 既有行为无回归
- **WHEN** 执行上一变更的既有验收
- **THEN** 登录、会话、角色权限、班级隔离、上传入库、下载与 `/health` 的行为保持不变
- **AND** 既有验收项全部通过，新增能力不改变已有状态码约定（跨班仍为 404）

#### Scenario: 未归档前不进入长期真相源
- **WHEN** 本变更仍有未通过的验收项
- **THEN** 本变更的规格 MUST NOT 出现在 `openspec/specs/`（长期真相源）
- **AND** 仅在全部验收通过后，才由归档流程写入 `openspec/specs/`
