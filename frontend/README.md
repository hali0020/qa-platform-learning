# QA Platform Learning Frontend

基于 Vue 3、TypeScript 和 Vite 的 QA 管理端，覆盖仪表盘、项目、测试套件、测试用例、计划与执行、缺陷、质量报表、流水线、任务和设备管理。

## 开发命令

```powershell
cd frontend
pnpm install
pnpm dev
```

质量检查：

```powershell
pnpm type-check
pnpm build
```

前端默认使用同源请求，不在浏览器构建变量中保存账号、Token、连接地址或其他环境信息。运行配置由未跟踪的本地环境提供。

## 目录

```text
src/
├── api/          # HTTP 客户端与领域契约
├── components/   # 通用组件
├── layouts/      # 应用布局与导航
├── router/       # 页面路由
├── styles/       # 全局样式
└── views/        # 业务页面
```
