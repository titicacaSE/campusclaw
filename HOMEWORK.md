# 第四课作业说明：班级限定知识库检索与内容溯源

> 仓库：CampusClaw（学期工程，Flask 3.x + SQLite 3）
> 本次变更：`add-class-scoped-knowledge-search`　新增能力：`class-knowledge-search`
> 变更目录：`openspec/changes/add-class-scoped-knowledge-search/`
> 变更状态：**规约已完成，功能未实现**（`tasks.md` 48 项全部 `[ ]`）

## 一、作业要求与本次交付

| 项 | 作业要求 | 本次交付 |
| --- | --- | --- |
| 变更描述 | 完成第四课 OpenSpec 变更描述 | ✅ 四件套 + `.openspec.yaml` |
| 检索范围 | **限定班级** | ✅ 规约为「班级过滤先于候选生成/匹配/排序/计数/limit」 |
| 内容溯源 | **支持溯源** | ✅ 规约为片段 + 材料/片段 ID + 码点偏移 + 行号 + 来源与下载链接 + 版本哈希 |
| 向量数据库 | **可选** | ✅ 明确**不采用**并给出理由与扩展点（`design.md` §8） |
| 严格校验 | `openspec validate --strict` | ⚠️ 本机无 `openspec` CLI，用仓库内等价校验并**如实记录**（见第三节） |
| 可公开仓库 | 提交公开仓库地址 | ⏸ **待补齐发布信息**（本机两仓均非 git 仓库，见第七节） |
| 本轮重点 | **变更描述**，不自动实施全部功能 | ✅ 未实施任何功能代码 |

## 二、变更内容概要

**为什么做**：上一变更已让材料能上传入库，但 `knowledge_entries` 每篇材料仅一行、`ord` 恒为 `0`、正文不分块、**没有任何偏移或行号坐标**，因此无法回答「这句话在哪份讲义的哪一段」，引用也无从回溯。

**本次新增什么**

1. `knowledge_chunks` 表：材料正文切分为可定位的块，记录块正文、相对全文的码点偏移、行号范围、块哈希与来源哈希。
2. `GET /api/knowledge/search`：班级限定检索。
3. `GET /api/knowledge/sources/<chunk_id>`：来源读取，**重新鉴权 + 核对正文版本**。
4. 索引生命周期：上传时同事务建索引；历史材料幂等回填；索引缺失或过期**显式降级**。
5. `/search` 页面：安全转义，区分空结果 / 降级 / 故障三种状态。

**不做什么**（Non-goals）：聊天、RAG 问答生成、作业流程、管理员、跨班检索、PDF/OCR、分词与语义向量检索、材料级 ACL。

## 三、严格校验结果（真实）

| 命令 | 真实结果 |
| --- | --- |
| `python scripts/validate_spec.py` | **0 error / 1 warning**（旧变更缺 `.openspec.yaml`） |
| `python scripts/validate_spec.py add-class-scoped-knowledge-search` | **0 error / 0 warning** |
| `python scripts/validate_spec.py add-class-scoped-knowledge-search --check-coverage` | **0 error / 0 warning** |
| `openspec validate add-class-scoped-knowledge-search --strict` | **未能执行：本机无 `openspec` CLI**（全局与 `npx` 均不可用） |

> **未执行就是未执行。** 上表第一列第三条的 CLI 命令在本机跑不起来，因此用仓库内 `scripts/validate_spec.py`
> 作等价校验（该脚本即为上一课为此目的编写，本次已泛化为支持任意变更名与覆盖检查）。
> **有 `openspec` CLI 的环境应补跑该命令复核**，不得以本地等价校验替代官方 CLI 的结论。

**校验器非空转的反向测试**（证明它能抓到问题，而非永远通过）：

| 人为制造的违规 | 是否被抓到 |
| --- | --- |
| 原始状态（全部 `[ ]`） | 0 error（正确） |
| 偷偷勾选 1 项任务 | ✅ 抓到（违反 `.openspec.yaml` 的 `allow_prechecked_tasks: false`） |
| 删除 `## ADDED Requirements` | ✅ 抓到（2 error） |
| 删除 proposal 五节 | ✅ 抓到（5 error） |

**已知局限**：覆盖检查只验证关键关切项**是否在 tasks.md 中被提及**，用于防止整块能力被遗漏；
它**不能**判断实现正确性，也无法发现"关键词仍在别处出现、但某一处被改坏"的情况。
行为正确性由 spec 的 Scenario 与 `tasks.md` 的 T7 验收用例负责。

**既有验收无回归**（本次改动未触碰功能代码）：

| 命令 | 结果 |
| --- | --- |
| `python tests/test_acceptance.py` | 23/23 **OK** |
| `python tests/http_acceptance.py` | 58/58 **通过** |
| `python scripts/check_compose.py` | 14/14 **通过** |

## 四、关键设计决策（摘要）

| # | 决策 | 选择 | 一句话理由 |
| --- | --- | --- | --- |
| D1 | 检索实现 | SQLite 字面子串匹配 | 中文无需分词即可复现；零新依赖；结果完全确定 |
| D2 | 分块 | 段落优先 + 512 码点硬切 | 与讲义结构一致，可精确到行 |
| D3 | 偏移坐标 | **Unicode 码点**，相对材料全文 | 与 Python `str` 一致，避免代理对歧义 |
| D4 | 归一化 | 入库 NFC；匹配时仅折叠 ASCII 大小写 | 折叠不改变长度 → 偏移恒精确 |
| D5 | 索引写入 | 与材料/正文**同事务** | 无中间态，失败可整体回滚 |
| D6 | 向量库 | **不采用** | 语义召回会把模型版本等不可复现因素带入验收面 |
| D7 | 溯源读取 | 独立端点，重新鉴权 | 内嵌全文会让过期片段看起来仍有效 |
| D8 | 来源过期 | `409 source_stale`，不返回正文 | 语义精确，前端不可能误当有效内容 |

**最关键的实现约束**：班级过滤 MUST 出现在候选生成 SQL 的**第一条谓词**上，
并先于匹配、计数、排序与 `limit`。`design.md` §3.3 给出了带序号的 SQL 示意，
并要求用「移除该谓词后隔离用例必须失败」作为**反向判据**。

## 五、本轮已完成的作业动作（含证据）

本节记录**本轮真实做过的事**，与 `tasks.md` 中「变更实现任务」严格区分——
`tasks.md` 的 48 项全部保持 `[ ]` 表示**功能确实未实现**，其语义为真。

| 动作 | 产物 | 证据 |
| --- | --- | --- |
| 读现有规约与代码，区分已实现/拟新增 | — | 见 `proposal.md`「前置依赖」表：5 项已实现、2 项待新增 |
| 编写变更四件套 | `proposal.md` / `design.md` / `specs/class-knowledge-search/spec.md` / `tasks.md` | 9 个 Requirement、43 个 Scenario、48 项任务 |
| 编写局部配置 | `.openspec.yaml` | 声明 `status: specified`、`depends_on`、`allow_prechecked_tasks: false` |
| 泛化并运行严格校验 | `scripts/validate_spec.py` | 支持任意变更名 + `--check-coverage`；新变更 0 error / 0 warning |
| 反向测试校验器 | — | 4 组人为违规均被抓到（见第三节） |
| 更新 README | `README.md` | 新增两个变更的状态表、规划中检索能力章节、数据库与验收命令 |
| 生成发布包 | `dist/campusclaw-public-*.zip` | 排除 `.env`、数据库、上传原件、Cookie、缓存；附 SHA-256 |
| 检查 Git 与发布身份 | — | **两仓均非 git 仓库，无远程**（见第七节） |

## 六、可复用提示词

以下提示词可直接粘贴到同类项目（CampusClaw 系列或其他 OpenSpec 项目）的 AI 编程助手中，
用于「写一个新变更的规约」，且**不会把"写规约"误当成"功能已实现"**。

```text
请在当前仓库完成一个新 OpenSpec 变更的规约描述，并严守「规约完成 ≠ 功能已实现」。

先读取 AGENTS.md、openspec/config.yaml，以及 openspec/changes/ 下已有变更的四件套，
再检查现有代码，明确区分【已实现能力】与【本次拟新增能力】。若外部资料缺失，明确说明资料限制，
依据既定要求完成，不编造课件或需求内容。

在 openspec/changes/<change-name>/ 新建：
1. proposal.md：Why / What Changes / Non-goals / Capabilities / Impact，
   并额外写明【前置依赖】（逐项标注已实现/待新增）与【交付状态】（明确标注"功能未实现"）。
2. design.md：数据结构、检索或核心算法、API、生命周期、溯源或定位、安全边界、取舍、迁移与回退。
3. specs/<capability>/spec.md：## ADDED Requirements，用 MUST/SHALL 与 WHEN/THEN/AND 场景写可验收行为。
4. tasks.md：按依赖拆成可执行任务，每项写 verify；【尚未实施的任务全部保持 [ ]】。
5. .openspec.yaml：遵循项目现有 schema，并声明 allow_prechecked_tasks: false。

设计硬性要求：
- 角色与数据边界一律来自服务端会话；客户端 query/body/header 不得参与授权。
- 边界过滤必须发生在【候选生成、匹配、排序、计数、top-k/limit 之前】，
  并写出反向判据：移除该过滤谓词后，隔离用例必须失败。
- 区分并写清：无权限、参数非法、不存在、状态过期、服务故障、无结果——各自的状态码与响应形态。
- 输入必须明确长度上限、匹配规则、排序规则、结果上限、无命中状态；禁止静默截断或改写输入。
- 每条结果提供可回溯的真实原文信息（来源标识、片段、偏移与坐标单位、行号、链接），
  并注明坐标定义与摘要对象；链接用相对路径，不写死主机名或本地路径。
- 派生数据（索引/缓存）必须可重建、可幂等回填；缺失或过期时不得伪装成"没有结果"。
- 失败路径不得留下脏数据；写入与派生数据必须一致提交。
- 明确 Non-goals 并在 spec 中写一条"范围纪律"需求，防止实现越界。
- 保留上一变更的真实完成状态，不得为未做的验收项勾选。

完成后：
- 运行严格校验（openspec validate <change-name> --strict；无 CLI 时用仓库内等价脚本并【如实记录】），
  做一次反向测试证明校验器不是空转。
- 检查需求 / 设计 / 任务一致性。
- 更新 README；写一份中文作业说明（含真实校验结果与完成度声明）。
- 准备发布包时排除 .env、真实密钥、数据库、上传原件、有效 Cookie、缓存与个人材料；
  并说明演示用凭据与真实凭据的区别。
- 检查 git 远程与发布身份；缺少平台/账号/仓库信息时只询问必要信息，同时完成本地交付；
  不得编造仓库 URL，不得把本地路径写成公开地址。
- 最后报告：变更目录、主要设计、真实校验结果、可复用提示词、公开仓库链接或剩余发布条件，
  并明确「规约完成」与「功能已实现」的区别。
```

**配套的收尾提示词**（本轮不执行，供后续实施使用）：

```text
请实施 openspec/changes/<change-name>。先读 AGENTS.md、该变更四件套及前置变更四件套，按 tasks 依赖顺序完成。
需求变化先更新本变更 Markdown。每完成一项执行其 verify，【真实通过才勾选】，并保存脱敏证据。
跑原有回归、新增隔离/检索/溯源/回填/故障测试。环境阻塞留空并写实际错误，不得用替代方式伪装验证通过。
全部任务及前置依赖完成后才归档到 openspec/specs。完成后报告已实现行为、测试证据、运行方式与仍未解决的限制。
```

## 七、发布包与剩余发布条件

### 发布包

生成于 `dist/`，内容仅含**可公开的源码、规约与说明**。

**排除项（MUST）**：`.env`（真实密钥）、`data/*.db`（含哈希口令的数据库）、`uploads/**`（上传原件）、
`__pycache__/`、`.venv/`、临时运行日志、任何有效 Cookie 或会话凭证。

**发布前须知（需人工确认的一项）**：`app/seed.py` 与 `README.md` 含**课程演示账号口令**
（如 `teacher_a` / `TeacherA@2026`）。保留原因：

- 它们是**演示数据**，批改者需要用它们登录验证；README 必须写明；
- `tests/` 直接 `from app.seed import DEMO_PASSWORDS`，移除会同时打断种子数据与全部测试。

它们是**课程演示凭据，不是真实凭据**。若你的公开仓库不允许出现任何明文口令，
可在发布前改为「种子口令从环境变量读取，本地用 `.env` 提供」——但这会改变已验收的行为，
需要按规约流程先改本变更 Markdown 再改代码。

### 剩余发布条件（需你提供）

检查结果：**`campusclaw/` 与 `campusclaw-semester/` 均不是 git 仓库**（`git rev-parse` 均返回
`fatal: not a git repository`），因此**无远程仓库、无已配置的发布身份**。

要完成公开发布，需要你提供（缺一不可）：

1. **目标仓库地址**（HTTPS 或 SSH，例如 `git@github.com:<账号>/<仓库>.git`）—— 我不会编造 URL；
2. **要发布的目录**：`campusclaw-semester/`（本校规工程）还是 `campusclaw/`（上一课的 Node 练习版）；
3. **提交身份**：用于 `git config user.name` / `user.email` 的名字与邮箱；
4. **可见性**：公开（public）还是私有；
5. 若使用 SSH 推送，确认可用的密钥（本机有两个 GitHub 账号，靠不同 key 区分）。

**在拿到上述信息前，本地交付已全部完成**：规约四件套 + 校验脚本 + README + 本说明 + 发布包。
不会把本地路径当作公开地址提交，也不会编造仓库链接。

## 八、完成度声明

| 问题 | 回答 |
| --- | --- |
| 变更描述是否完成？ | **是**。四件套 + `.openspec.yaml` 就位，严格校验 0 error。 |
| 功能是否已实现？ | **否**。无任何实现代码，`tasks.md` 48 项全部 `[ ]`。 |
| 是否已归档到 `openspec/specs/`？ | **否**。规约要求全部验收通过后才可归档。 |
| 上一变更的完成状态是否被改动？ | **未改动**，未为其勾选任何未做项。 |
| 公开仓库是否已发布？ | **否**，等待第七节的 5 项信息。 |

> **「规约完成」≠「功能已实现」。** 本变更目前只描述"将要交付什么行为、如何判定通过"，
> 仓库中尚无 `knowledge_chunks` 表、无检索接口、无 `/search` 页面、无回填脚本、无对应测试。
