#!/usr/bin/env python3
"""规约自检（本地等价于 `openspec validate <change> --strict`）

用法：
  python scripts/validate_spec.py                              # 校验 openspec/changes 下全部 change
  python scripts/validate_spec.py <change-name>                # 只校验指定 change
  python scripts/validate_spec.py <change-name> --check-coverage  # 追加任务覆盖检查

检查项：
  1. change 目录含四件套：proposal.md、design.md、tasks.md、specs/<capability>/spec.md
  2. proposal.md 含 Why / What Changes / Non-goals / Capabilities / Impact 五节
  3. spec.md 以 `## ADDED Requirements` 标记 delta，每个 Requirement 至少 1 个 Scenario
  4. 每个 Scenario 用 WHEN / THEN（AND 建议）书写
  5. tasks.md 顶层条目均为 checkbox、缩进注解行（verify:）合法、至少 5 个 `## T<n>` 分组、
     且 verify: 条数不少于任务数
  6. 若 .openspec.yaml 声明 `allow_prechecked_tasks: false`，则 tasks.md 不得有任何已勾选项
  7. （--check-coverage）关键关切项在 tasks.md 中有对应任务

**覆盖检查的局限（已知且刻意）**：`--check-coverage` 只验证每个关切项**是否在 tasks.md 中被提及**，
用于防止"整块能力被遗漏"；它**不能**判断实现正确性，也无法发现"关键词仍在别处出现、
但某一处被删改"的情况。真正的行为正确性由 spec 的 Scenario 与 T7 的验收用例负责。

说明：本机无 openspec CLI，本脚本提供**等价校验**。有 CLI 的环境应另跑
`openspec validate <change> --strict` 复核。
退出码：0 = 无 error；1 = 存在 error。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# 控制台编码兜底：中文 Windows 的默认 stdout 编码是 GBK，混合输出中文与符号时
# 可能抛 UnicodeEncodeError，让校验脚本以「崩掉」而非「报告结论」收场。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
CHANGES_DIR = ROOT / "openspec" / "changes"

PROPOSAL_SECTIONS = ("Why", "What Changes", "Non-goals", "Capabilities", "Impact")

# --check-coverage：本变更必须覆盖的关键关切项（任一关键词命中即视为覆盖）
COVERAGE_CONCERNS = (
    ("班级过滤先于匹配", ("class_id",)),
    ("会话鉴权 401", ("401",)),
    ("角色拒绝 403", ("403",)),
    ("参数校验 400", ("400",)),
    ("跨班同形 404", ("404",)),
    ("来源过期 409", ("409", "stale")),
    ("索引降级信号", ("degraded",)),
    ("历史材料回填", ("backfill_index", "回填")),
    ("前端输出转义", ("转义", "escap")),
    ("排序与结果上限", ("排序", "limit")),
    ("溯源坐标", ("start_offset", "偏移")),
    ("分块与边界", ("分块", "chunk")),
    ("既有行为回归", ("回归",)),
    ("真实证据", ("verify",)),
    # —— add-traceable-vector-retrieval 新增关切项 ——
    ("三种检索模式", ("mode", "keyword", "hybrid")),
    ("RRF 融合 k=60", ("rrf", "60")),
    ("向量相似度阈值", ("0.35", "阈值")),
    ("切分策略", ("chunk_strategy", "hierarchy")),
    ("显式重建索引", ("reindex", "重建")),
    ("index_status 状态机", ("index_status",)),
    ("无命中不调生成模型", ("no_evidence", "固定文案")),
    ("向量库降级 503", ("503", "qdrant")),
    ("system 提示由服务端写入", ("system",)),
    ("引用与 citations 顺序", ("citation",)),
    ("网关密钥只在服务端", ("密钥", "secret")),
)


def _load_min_yaml(path: Path) -> dict:
    """读取 .openspec.yaml；优先用 pyyaml，缺失时用极简解析。"""
    try:
        import yaml  # type: ignore

        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        data: dict = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
            if m:
                data[m.group(1)] = m.group(2).strip()
        return data


def validate_change(change_dir: Path, check_coverage: bool) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    name = change_dir.name

    # 1) 四件套
    for fname in ("proposal.md", "design.md", "tasks.md"):
        if not (change_dir / fname).is_file():
            errors.append(f"[{name}] 缺少 {fname}")

    specs_dir = change_dir / "specs"
    spec_files = sorted(specs_dir.glob("*/spec.md")) if specs_dir.is_dir() else []
    if not spec_files:
        errors.append(f"[{name}] specs/<capability>/spec.md 缺失")

    # 2) proposal.md 五节
    proposal_path = change_dir / "proposal.md"
    proposal = proposal_path.read_text(encoding="utf-8") if proposal_path.is_file() else ""
    for section in PROPOSAL_SECTIONS:
        if not re.search(rf"^#+\s*{re.escape(section)}\b", proposal, re.M):
            errors.append(f"[{name}] proposal.md 缺少小节：{section}")

    # 3)(4) spec delta 与 Scenario
    for spec in spec_files:
        text = spec.read_text(encoding="utf-8")
        try:
            rel = spec.relative_to(ROOT)
        except ValueError:  # 校验仓库外的副本时（如反向测试）退化为绝对路径
            rel = spec
        if not re.search(r"^##\s+ADDED Requirements\s*$", text, re.M):
            errors.append(f"[{rel}] 缺少 `## ADDED Requirements` 标题（delta 标记）")
        requirements = re.findall(r"^###\s+Requirement:\s*(.+)$", text, re.M)
        if not requirements:
            errors.append(f"[{rel}] 未找到 `### Requirement:` 条目")
        scenarios = re.findall(r"^####\s+Scenario:\s*(.+)$", text, re.M)
        if len(scenarios) < len(requirements):
            errors.append(
                f"[{rel}] Scenario 数量（{len(scenarios)}）少于 Requirement 数量（{len(requirements)}）"
            )
        blocks = re.split(r"^####\s+Scenario:", text, flags=re.M)[1:]
        for idx, block in enumerate(blocks, start=1):
            if not (re.search(r"\bWHEN\b|当", block) and re.search(r"\bTHEN\b|那么", block)):
                errors.append(f"[{rel}] 第 {idx} 个 Scenario 缺少 WHEN/THEN 结构")
            if not re.search(r"\bAND\b|且", block):
                warnings.append(f"[{rel}] 第 {idx} 个 Scenario 未使用 AND 补充条件")

    # 5) tasks.md
    tasks_path = change_dir / "tasks.md"
    tasks = ""
    if tasks_path.is_file():
        tasks = tasks_path.read_text(encoding="utf-8")
        checkboxes = 0
        checked = 0
        for line in tasks.splitlines():
            if not line.strip() or line.lstrip() != line:
                continue  # 缩进行 = 上一条任务的注解（OpenSpec tasks 允许 verify: 子条目）
            stripped = line.strip()
            if stripped.startswith(("#", ">", "|", "```")) or stripped == "---":
                continue
            if re.match(r"^- \[[ xX]\] ", stripped):
                checkboxes += 1
                if re.match(r"^- \[[xX]\] ", stripped):
                    checked += 1
            else:
                errors.append(f"[{name}] tasks.md 存在非 checkbox 顶层行：{stripped[:60]}")
        groups = re.findall(r"^##\s+T(\d+)\b", tasks, re.M)
        if len(groups) < 5:
            errors.append(f"[{name}] tasks.md 任务分组不足（找到 {len(groups)} 个 `## T<n>`，至少需 5 个）")
        verify_count = len(re.findall(r"^\s*-\s*verify:", tasks, re.M))
        if verify_count < checkboxes:
            errors.append(
                f"[{name}] tasks.md 的 verify: 条数（{verify_count}）少于任务数（{checkboxes}）"
                "：每条任务 MUST 附带可执行的 verify:"
            )
    else:
        checkboxes = checked = 0

    # 6) .openspec.yaml：存在则校验，缺失仅告警（兼容早期变更）
    cfg: dict = {}
    cfg_path = change_dir / ".openspec.yaml"
    if cfg_path.is_file():
        cfg = _load_min_yaml(cfg_path)
        for key in ("project", "change", "capability", "status", "artifacts"):
            if key not in cfg:
                errors.append(f"[{name}] .openspec.yaml 缺少键：{key}")
        if str(cfg.get("change", "")).strip() not in ("", name):
            errors.append(f"[{name}] .openspec.yaml 的 change 与目录名不一致：{cfg.get('change')}")
        rules = cfg.get("rules") or {}
        if isinstance(rules, dict) and rules.get("allow_prechecked_tasks") is False and checked:
            errors.append(
                f"[{name}] .openspec.yaml 声明 allow_prechecked_tasks: false，"
                f"但 tasks.md 已有 {checked} 项被勾选（未实施任务 MUST 保持 [ ]）"
            )
    else:
        warnings.append(f"[{name}] 缺少 .openspec.yaml（建议为每个变更添加）")

    # 7) 覆盖检查
    #    关切项可由 `.openspec.yaml` 的 `coverage_concerns` 覆盖（每个变更的关切面不同，
    #    用一张全局表会把「变更二已实现的检索项」强加给「变更三的智能体/技能/MCP」）。
    if check_coverage:
        concerns = COVERAGE_CONCERNS
        if isinstance(cfg, dict) and isinstance(cfg.get("coverage_concerns"), (list, tuple)):
            parsed = []
            for item in cfg["coverage_concerns"]:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    parsed.append((str(item[0]), tuple(str(x) for x in item[1])))
            if parsed:
                concerns = tuple(parsed)
        missing = [
            label
            for label, tokens in concerns
            if not any(tok.lower() in tasks.lower() for tok in tokens)
        ]
        for label in missing:
            errors.append(f"[{name}] 任务覆盖检查：tasks.md 未见与「{label}」相关的任务")

    return errors, warnings


def main() -> int:
    args = [a for a in sys.argv[1:]]
    check_coverage = "--check-coverage" in args
    args = [a for a in args if not a.startswith("--")]

    if not CHANGES_DIR.is_dir():
        print(f"error: 找不到 {CHANGES_DIR}")
        return 1

    if args:
        targets = [CHANGES_DIR / args[0]]
        if not targets[0].is_dir():
            print(f"error: change 目录不存在：{targets[0]}")
            return 1
    else:
        targets = sorted(p for p in CHANGES_DIR.iterdir() if p.is_dir())

    errors: list[str] = []
    warnings: list[str] = []
    for target in targets:
        e, w = validate_change(target, check_coverage)
        errors += e
        warnings += w

    for w in warnings:
        print(f"warn:  {w}")
    for e in errors:
        print(f"error: {e}")

    names = ", ".join(t.name for t in targets)
    suffix = "（含覆盖检查）" if check_coverage else ""
    if errors:
        print(f"\n规约校验失败：{len(errors)} 个 error（{len(targets)} 个变更）")
        return 1
    print(
        f"规约校验通过（strict）{suffix}：{len(targets)} 个变更 [{names}]"
        f" —— 0 error, {len(warnings)} warning"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
