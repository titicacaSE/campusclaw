"""接口测试运行器：一接口一脚本 → 一份 HTML 报告。

用法
----
  python apitest/run_all.py --base-url http://127.0.0.1:5000
  python apitest/run_all.py --base-url http://127.0.0.1:9 --dry-run    # 只列出计划，不联网
  python apitest/run_all.py --out apitest/report.html

设计
----
  - 脚本按文件名排序执行，前一个脚本可以把上下文写进 `ctx`（如 `agent_id`），
    后一个脚本直接用 —— ask/stream 需要先建会话，就是这个机制；
  - `--dry-run` 不发起任何请求，因此可以在服务没起来时验证「脚本齐不齐、顺序对不对」；
  - 报告是单文件 HTML，不含外链，方便直接归档进作业。
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import importlib.util
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _client import ApiClient, DEMO_PASSWORDS, run_spec  # noqa: E402

SKIP_PREFIX = "_"


def discover() -> list[tuple[str, object]]:
    modules = []
    for path in sorted(HERE.glob("api_*.py")):
        name = path.stem
        spec = importlib.util.spec_from_file_location(f"apitest_{name}", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        modules.append((name, module))
    return modules


def path_of(spec: dict) -> str:
    path = spec.get("path")
    return path if isinstance(path, str) else "<运行时决定>"


def main() -> int:
    parser = argparse.ArgumentParser(description="接口测试：一接口一脚本")
    parser.add_argument("--base-url", default="http://127.0.0.1:5000")
    parser.add_argument("--dry-run", action="store_true", help="只列出计划，不发起请求")
    parser.add_argument("--out", default=str(HERE / "report.html"))
    parser.add_argument("--username", default="teacher_a")
    parser.add_argument(
        "--mcp-token",
        default=None,
        help="MCP 服务令牌（只从命令行/环境变量传入，不写进任何文件）。缺省时该用例只验证 401 边界。",
    )
    args = parser.parse_args()

    modules = discover()
    print(f"发现 {len(modules)} 个接口脚本（{args.base_url}）\n")

    if args.dry_run:
        for index, (name, module) in enumerate(modules, start=1):
            spec = getattr(module, "SPEC")
            print(
                f"  {index:>2}. [{spec['method']:>6}] {path_of(spec):<48} "
                f"{spec['name']}  期望 {spec['expect']}"
            )
        print(f"\n[dry-run] 共 {len(modules)} 个脚本，未发起任何请求。")
        return 0

    client = ApiClient(args.base_url)
    try:
        client.login(args.username)
    except Exception as exc:  # noqa: BLE001
        print(f"无法登录 {args.username}：{exc}")
        print("请确认服务已启动，且已写入种子数据（scripts/init_db.py）。")
        return 2

    ctx: dict = {}
    if args.mcp_token:
        ctx["mcp_token"] = args.mcp_token
    results: list[dict] = []
    started_at = dt.datetime.now()

    for name, module in modules:
        spec = getattr(module, "SPEC")
        print(f"[{spec['name']}] {spec['method']} {path_of(spec)} ... ", end="")
        t0 = time.perf_counter()
        try:
            if hasattr(module, "run"):
                ok, detail, elapsed = module.run(client, ctx)
            else:
                ok, detail, elapsed = run_spec(spec, client, ctx)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
            elapsed = int((time.perf_counter() - t0) * 1000)
        print("PASS" if ok else "FAIL")
        results.append(
            {
                "file": name, "name": spec["name"], "method": spec["method"],
                "path": path_of(spec), "expect": ",".join(str(x) for x in spec["expect"]),
                "note": spec.get("note", ""), "ok": ok, "detail": detail, "ms": elapsed,
            }
        )

    finished_at = dt.datetime.now()
    passed = sum(1 for r in results if r["ok"])
    html_path = Path(args.out)
    html_path.write_text(
        _render(results, args.base_url, started_at, finished_at), encoding="utf-8"
    )

    print(f"\n结果：{passed}/{len(results)} 通过")
    print(f"报告：{html_path}")
    return 0 if passed == len(results) else 1


def _render(results: list[dict], base_url: str, started: dt.datetime,
            finished: dt.datetime) -> str:
    passed = sum(1 for r in results if r["ok"])
    failed = len(results) - passed
    rows = []
    for index, item in enumerate(results, start=1):
        cls = "pass" if item["ok"] else "fail"
        rows.append(
            f"<tr class='{cls}'>"
            f"<td>{index}</td>"
            f"<td>{html.escape(item['name'])}</td>"
            f"<td><code>{item['method']}</code></td>"
            f"<td><code>{html.escape(item['path'])}</code></td>"
            f"<td>{html.escape(item['expect'])}</td>"
            f"<td>{'通过' if item['ok'] else '失败'}</td>"
            f"<td>{item['ms']} ms</td>"
            f"<td class='detail'>{html.escape(item['detail'])}</td>"
            f"<td class='note'>{html.escape(item['note'])}</td>"
            f"</tr>"
        )
    duration = (finished - started).total_seconds()
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>接口测试报告 · CampusClaw AI 应用平台</title>
<style>
 body{{font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;margin:32px;color:#1f2933;background:#fbfcfd}}
 h1{{font-size:20px;margin:0 0 6px}}
 .meta{{color:#52606d;font-size:13px;margin-bottom:18px}}
 .cards{{display:flex;gap:14px;margin:18px 0 22px}}
 .card{{border:1px solid #e2e8f0;border-radius:10px;padding:12px 18px;background:#fff;min-width:130px}}
 .card b{{display:block;font-size:22px;margin-top:4px}}
 .ok b{{color:#0f766e}} .bad b{{color:#b91c1c}}
 table{{border-collapse:collapse;width:100%;background:#fff;font-size:13px}}
 th,td{{border:1px solid #e2e8f0;padding:7px 9px;text-align:left;vertical-align:top}}
 th{{background:#f1f5f9;font-weight:600}}
 tr.fail td{{background:#fef2f2}}
 td.detail{{max-width:420px;word-break:break-all;font-family:Consolas,monospace;font-size:12px}}
 td.note{{max-width:220px;color:#52606d}}
 code{{background:#f1f5f9;padding:1px 5px;border-radius:4px}}
 .foot{{margin-top:16px;color:#7b8794;font-size:12px}}
</style></head><body>
<h1>CampusClaw AI 应用平台 · 接口测试报告</h1>
<div class="meta">
 目标实例 <code>{html.escape(base_url)}</code> ·
 开始 {started:%Y-%m-%d %H:%M:%S} · 耗时 {duration:.1f}s ·
 脚本 {len(results)} 个
</div>
<div class="cards">
  <div class="card ok">通过<b>{passed}</b></div>
  <div class="card bad">失败<b>{failed}</b></div>
  <div class="card">通过率<b>{passed / len(results) * 100:.0f}%</b></div>
</div>
<table>
<thead><tr>
 <th>#</th><th>接口</th><th>方法</th><th>路径</th><th>期望</th>
 <th>结果</th><th>耗时</th><th>响应摘要</th><th>说明</th>
</tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
<div class="foot">
 由 <code>apitest/run_all.py</code> 生成 · 一接口一脚本，脚本只声明 SPEC，运行器统一执行与记录。
</div>
</body></html>
"""


if __name__ == "__main__":
    sys.exit(main())
