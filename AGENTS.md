# AGENTS.md —— CampusClaw 项目规则

## 第一条：无规约不写代码

任何功能变更之前，先在本仓库 `openspec/changes/<change-name>/` 下写清四件套：

```
openspec/changes/<change-name>/
├── proposal.md                  # Why / What Changes / Non-goals / Capabilities / Impact
├── design.md                    # 技术决策、会话与密码、服务端隔离、数据流、部署
├── specs/<capability>/spec.md    # `## ADDED Requirements` + Requirement/Scenario（WHEN/THEN/AND）
└── tasks.md                     # 分节 checklist，每条附 verify: 可执行命令
```

未在 `openspec/` 中写下的约定视为无效；口头或聊天记录中的约定不作为验收依据。
`openspec/` 是唯一事实来源，换一个会话只读它即可复现全部要求。

## 第二条：每条需求必须可判定

写需求时同时写出「怎么判定通过/不通过」。负路径（错误密码、未登录、跨班、学生上传）与正路径同等重要。

## 第三条：安全边界在服务端

- 班级是数据边界：`class_id` 只取自会话，所有查询强制 `WHERE class_id = ?`；
- 不信任客户端传入的 `class_id` / `class` 字段；
- 前端隐藏按钮不作为权限手段，写接口一律由服务端鉴权；
- 口令只存 bcrypt 哈希；密钥只从环境变量注入。

## 第四条：改完必须自证

```bash
python tests/test_acceptance.py     # 场景级
python tests/http_acceptance.py     # 真实 HTTP + SQL 计数
python scripts/validate_spec.py     # 规约校验
```

三项全绿才算完成；结果需写入 `ACCEPTANCE.md` 并引用 spec 原文。

## 第五条：范围纪律

`proposal.md` 的 Non-goals 是硬边界。表结构可预留字段，但不实现 Non-goals 中的能力，
也不把课表练习仓（`sdd-lab-timetable`）的内容混入本仓库。
