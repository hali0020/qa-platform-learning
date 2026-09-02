# 独立 Pytest 学习网站测试包

该目录独立于 `backend/tests`，从使用者视角验证 QA Platform Learning 的公开 HTTP 路径、核心业务旅程和横切质量属性。

## 执行

```powershell
cd qa-platform-learning
.\backend\.venv\Scripts\python.exe -m pytest pytest -q
.\backend\.venv\Scripts\python.exe pytest\build_report.py
```

输出：

- `pytest/reports/raw_results.json`：pytest 结果与 HTTP 路径观测原始数据；
- `pytest/reports/pytest_learning_site_report.html`：离线可视化报告。

## 指标边界

- 路径覆盖率：实际命中的 FastAPI 路由模板 / 本测试范围内公开路由模板；它不是代码行覆盖率。
- 通过率：passed / (passed + failed)；跳过项单列，避免抬高或压低可判定结果。
- P50/P95/P99：测试用例耗时分位数，用于发现慢用例和回归信号，不等同于生产接口 SLA。
- 状态码分布：验证成功、校验失败、资源不存在、冲突等路径都被真实触达。

