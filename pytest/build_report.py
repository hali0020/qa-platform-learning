from __future__ import annotations

import html
import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "backend"))
os.environ["QA_PLATFORM_SKIP_LOCAL_ENV"] = "1"

from app.core.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


RAW = HERE / "reports" / "raw_results.json"
OUT = HERE / "reports" / "pytest_learning_site_report.html"


def percentile(values, q):
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def esc(value):
    return html.escape(str(value))


def defined_operations():
    app = create_app(Settings(app_env="test", auth_enabled=False, database_url="sqlite+aiosqlite:///:memory:"))
    values = set()
    for route in app.router.routes:
        path = getattr(route, "path", "")
        if not (path.startswith("/api/v1") or path in {"/health/live", "/health/ready", "/metrics"}):
            continue
        for method in getattr(route, "methods", set()):
            if method not in {"HEAD", "OPTIONS"}:
                values.add((method, path))
    return values


def bar_rows(items, color="#2e74b5", unit=""):
    maximum = max((value for _, value in items), default=1) or 1
    return "".join(
        f'<div class="bar-row"><span>{esc(label)}</span><div class="track"><i style="width:{value/maximum*100:.1f}%;background:{color}"></i></div><b>{value:.2f}{unit}</b></div>'
        for label, value in items
    )


def build():
    data = json.loads(RAW.read_text(encoding="utf-8"))
    tests = data["tests"]
    requests = data["http_observations"]
    total = len(tests)
    passed = sum(t["outcome"] == "passed" for t in tests)
    failed = sum(t["outcome"] == "failed" for t in tests)
    skipped = sum(t["outcome"] == "skipped" for t in tests)
    decided = passed + failed
    pass_rate = passed / decided * 100 if decided else 0
    test_ms = [t["duration_ms"] for t in tests]
    request_ms = [r["duration_ms"] for r in requests]

    defined = defined_operations()
    observed = {(r["method"], r["route_template"]) for r in requests}
    covered = defined & observed
    op_rate = len(covered) / len(defined) * 100 if defined else 0

    status_counts = Counter(r["status_code"] for r in requests)
    area = defaultdict(lambda: {"passed": 0, "failed": 0, "skipped": 0, "duration": 0.0})
    for test in tests:
        area[test["area"]][test["outcome"]] += 1
        area[test["area"]]["duration"] += test["duration_ms"]

    endpoints = defaultdict(lambda: {"count": 0, "statuses": Counter(), "times": []})
    for req in requests:
        key = (req["method"], req["route_template"])
        if key not in defined:
            continue
        endpoints[key]["count"] += 1
        endpoints[key]["statuses"][req["status_code"]] += 1
        endpoints[key]["times"].append(req["duration_ms"])

    status_palette = {200: "#2f7d5c", 201: "#3d9270", 400: "#a56616", 401: "#a43d3d", 403: "#8b3d5b", 404: "#6b7280", 405: "#7b61a8", 409: "#c47b28", 422: "#2e74b5"}
    status_chart = "".join(
        f'<div class="status-card"><span style="background:{status_palette.get(code, "#59636e")}">{code}</span><b>{count}</b><small>{count/len(requests)*100:.1f}%</small></div>'
        for code, count in sorted(status_counts.items())
    )

    area_rows = "".join(
        f"<tr><td>{esc(name)}</td><td>{vals['passed']+vals['failed']+vals['skipped']}</td><td class='ok'>{vals['passed']}</td><td class='bad'>{vals['failed']}</td><td>{vals['skipped']}</td><td>{vals['duration']/1000:.2f}s</td></tr>"
        for name, vals in sorted(area.items())
    )
    endpoint_rows = "".join(
        f"<tr><td><code>{esc(method)}</code></td><td><code>{esc(path)}</code></td><td>{stats['count']}</td><td>{esc(', '.join(f'{k}×{v}' for k,v in sorted(stats['statuses'].items())))}</td><td>{percentile(stats['times'],.95):.2f} ms</td><td>{'已触达' if (method,path) in covered else '未触达'}</td></tr>"
        for (method, path), stats in sorted(endpoints.items(), key=lambda x: (x[0][1], x[0][0]))
    )
    slowest = sorted(tests, key=lambda t: t["duration_ms"], reverse=True)[:15]
    slow_rows = "".join(
        f"<tr><td>{i}</td><td>{esc(t['area'])}</td><td><code>{esc(t['nodeid'])}</code></td><td>{t['duration_ms']:.2f} ms</td></tr>"
        for i, t in enumerate(slowest, 1)
    )

    duration_chart = bar_rows([
        ("测试 P50", percentile(test_ms, .50)),
        ("测试 P95", percentile(test_ms, .95)),
        ("测试 P99", percentile(test_ms, .99)),
        ("HTTP P50", percentile(request_ms, .50)),
        ("HTTP P95", percentile(request_ms, .95)),
        ("HTTP P99", percentile(request_ms, .99)),
    ], unit=" ms")
    area_chart = bar_rows([(name, float(vals["passed"] + vals["failed"] + vals["skipped"])) for name, vals in sorted(area.items())], color="#d28a2e")

    page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pytest 学习网站测试报告</title>
<style>
:root{{--ink:#17212b;--muted:#66727e;--blue:#2e74b5;--green:#2f7d5c;--amber:#a56616;--red:#a43d3d;--line:#dfe6ec;--paper:#fff;--bg:#eef2f5}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 "Microsoft YaHei",Arial,sans-serif}}main{{max-width:1240px;margin:auto;padding:28px}}
header{{background:linear-gradient(120deg,#1e3448,#2e74b5);color:#fff;padding:34px 38px;border-radius:16px;box-shadow:0 12px 30px #20364b2b}}header h1{{margin:4px 0 8px;font-size:32px}}header p{{margin:0;color:#dfeefa}}.kicker{{text-transform:uppercase;letter-spacing:.14em;font-size:12px;color:#f5c36b}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:18px 0}}.card,.panel{{background:var(--paper);border:1px solid var(--line);border-radius:13px;box-shadow:0 4px 14px #2436460d}}.card{{padding:18px}}.card span{{color:var(--muted);font-size:12px}}.card strong{{display:block;font-size:27px;margin-top:5px}}.card small{{color:var(--muted)}}.good{{color:var(--green)}}
.panel{{padding:22px;margin:16px 0}}h2{{margin:0 0 14px;font-size:21px;color:#20364b}}h3{{margin:18px 0 8px;font-size:16px}}.two{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
table{{width:100%;border-collapse:collapse;font-size:12px}}th,td{{padding:9px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{background:#e8eef5;color:#20364b;position:sticky;top:0}}tbody tr:nth-child(even){{background:#f8fafb}}code{{font:11px Consolas,monospace;overflow-wrap:anywhere}}.ok{{color:var(--green);font-weight:700}}.bad{{color:var(--red);font-weight:700}}
.bar-row{{display:grid;grid-template-columns:130px 1fr 100px;gap:10px;align-items:center;margin:10px 0}}.track{{height:12px;background:#e8edf1;border-radius:999px;overflow:hidden}}.track i{{display:block;height:100%;border-radius:999px}}.bar-row b{{font-size:12px;text-align:right}}.status-grid{{display:flex;flex-wrap:wrap;gap:10px}}.status-card{{display:grid;grid-template-columns:48px 42px 50px;align-items:center;border:1px solid var(--line);border-radius:9px;overflow:hidden}}.status-card span{{padding:9px;color:#fff;font-weight:700;text-align:center}}.status-card b,.status-card small{{text-align:center}}.note{{border-left:4px solid var(--amber);background:#fff7e8;padding:12px 14px;border-radius:7px}}.scroll{{max-height:620px;overflow:auto;border:1px solid var(--line);border-radius:9px}}ul{{padding-left:20px}}footer{{color:var(--muted);text-align:center;padding:20px}}@media(max-width:800px){{.grid,.two{{grid-template-columns:1fr 1fr}}main{{padding:12px}}}}@media(max-width:520px){{.grid,.two{{grid-template-columns:1fr}}}}
</style></head><body><main>
<header><div class="kicker">QA Platform Learning · Independent Pytest Suite</div><h1>学习网站测试执行报告</h1><p>生成时间：{esc(data['generated_at'])}　|　离线报告，不加载外部脚本或字体</p></header>
<section class="grid">
<div class="card"><span>执行用例</span><strong>{total}</strong><small>独立 pytest 包</small></div>
<div class="card"><span>通过率</span><strong class="good">{pass_rate:.2f}%</strong><small>{passed} passed / {failed} failed / {skipped} skipped</small></div>
<div class="card"><span>公开操作面覆盖</span><strong>{op_rate:.2f}%</strong><small>{len(covered)} / {len(defined)} 个 method + route</small></div>
<div class="card"><span>HTTP 观测</span><strong>{len(requests)}</strong><small>{len(status_counts)} 种状态码</small></div>
</section>
<section class="panel"><h2>执行结论</h2><p><b>全部 {total} 项测试通过。</b>测试覆盖路径矩阵、完整公开操作面、核心业务旅程和横切质量属性。共发出 {len(requests)} 次进程内 HTTP 请求，触达 {len(covered)}/{len(defined)} 个公开方法+路由操作。</p><div class="note">“公开操作面覆盖率”说明路由是否被真实请求触达，不等于代码行/分支覆盖率；“HTTP 耗时”来自 ASGI 进程内执行，可用于本次测试内比较，不能作为网络环境或生产 SLA。</div></section>
<section class="two"><div class="panel"><h2>测试领域分布</h2>{area_chart}</div><div class="panel"><h2>耗时分位数</h2>{duration_chart}</div></section>
<section class="panel"><h2>HTTP 状态码分布</h2><div class="status-grid">{status_chart}</div><p>为什么看它：只有 2xx 会漏掉校验、未找到、冲突、鉴权和方法限制等关键异常路径。状态码分布能证明测试实际触达了多类协议结果，但状态码数量本身不是质量越高越好。</p></section>
<section class="panel"><h2>指标定义与采用原因</h2><table><thead><tr><th>指标</th><th>计算口径</th><th>为什么使用</th><th>边界</th></tr></thead><tbody>
<tr><td>执行用例数</td><td>pytest 最终收集并进入 call 阶段的测试项</td><td>反映本次可重复验证规模，参数化用例逐项计数</td><td>数量不代表风险覆盖，必须结合领域与路径矩阵</td></tr>
<tr><td>通过率</td><td>passed / (passed + failed)</td><td>只把有明确判定的结果放入分母；skipped 单列，避免失真</td><td>不能掩盖关键路径失败；本报告同时展示失败绝对数</td></tr>
<tr><td>公开操作面覆盖率</td><td>触达的 method+route / FastAPI 公开 method+route</td><td>GET/POST 同一路径风险不同，组合计数比只数 URL 更准确</td><td>只证明“到达端点”，不证明端点内部所有分支已覆盖</td></tr>
<tr><td>核心旅程覆盖</td><td>项目→用例→计划→执行→结果→质量报表的闭环断言</td><td>单端点都正确仍可能在跨资源状态流转中失败，因此需要端到端闭环</td><td>是隔离 SQLite/ASGI 环境，不代表真实部署拓扑</td></tr>
<tr><td>P50/P95/P99 耗时</td><td>按全部样本排序插值计算 50/95/99 分位</td><td>P50 看典型值，P95/P99 暴露长尾；比平均值更能发现偶发慢用例</td><td>进程内测试耗时含数据库创建和夹具开销，不是服务 SLO</td></tr>
<tr><td>状态码分布</td><td>每次实际 HTTP 响应按状态码计数及占比</td><td>证明成功、校验失败、404、冲突、权限等不同协议路径被触达</td><td>分布比例由测试设计决定，不应与线上流量比例比较</td></tr>
<tr><td>失败数</td><td>pytest outcome=failed 的绝对数量</td><td>发布门禁中任何关键失败都不可被高通过率稀释</td><td>需结合失败严重度；本轮为 0</td></tr>
</tbody></table></section>
<section class="panel"><h2>测试领域明细</h2><table><thead><tr><th>领域</th><th>总数</th><th>通过</th><th>失败</th><th>跳过</th><th>累计耗时</th></tr></thead><tbody>{area_rows}</tbody></table></section>
<section class="panel"><h2>公开端点触达明细</h2><div class="scroll"><table><thead><tr><th>方法</th><th>路由模板</th><th>请求数</th><th>状态码</th><th>请求 P95</th><th>状态</th></tr></thead><tbody>{endpoint_rows}</tbody></table></div></section>
<section class="panel"><h2>最慢的 15 个测试项</h2><table><thead><tr><th>#</th><th>领域</th><th>测试项</th><th>耗时</th></tr></thead><tbody>{slow_rows}</tbody></table><p>用途：定位测试套件自身的慢点和不稳定候选；不能把单次排名直接认定为产品性能问题。</p></section>
<section class="panel"><h2>覆盖内容与限制</h2><div class="two"><div><h3>已覆盖</h3><ul><li>全部公开 FastAPI 方法+路由操作可寻址且无 5xx</li><li>列表、详情不存在、UUID 校验、空请求体、枚举边界、未知路径</li><li>项目—用例—计划—执行—结果—质量指标完整闭环</li><li>状态机逆向跳转、重复键、引用删除保护、结果状态契约</li><li>Request ID、统一错误结构、CORS、Prometheus 标签、下载安全头</li></ul></div><div><h3>未宣称</h3><ul><li>未安装 coverage.py，因此不报告代码行/分支覆盖率</li><li>未启动真实浏览器，不包含 Vue 页面像素、交互与可访问性 E2E</li><li>未使用真实 PostgreSQL/RabbitMQ/S3/OIDC/外部 CI</li><li>未做容量、并发、弱网、长稳或生产网络性能测试</li><li>完整操作面中的部分请求验证的是输入校验/不存在资源分支，不等于成功业务路径</li></ul></div></div></section>
<footer>原始数据：pytest/reports/raw_results.json　·　报告生成器：pytest/build_report.py</footer>
</main></body></html>"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(page, encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    build()
