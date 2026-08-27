import { createRouter, createWebHistory } from "vue-router";
import { useAuthSession } from "@/auth/session";

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: "/login", component: () => import("@/views/LoginView.vue"), meta: { title: "登录", public: true, standalone: true } },
    { path: "/forbidden", component: () => import("@/views/ForbiddenView.vue"), meta: { title: "没有权限" } },
    { path: "/", component: () => import("@/views/DashboardView.vue"), meta: { title: "质量仪表盘", permission: "qa.read" } },
    { path: "/projects", component: () => import("@/views/ProjectsView.vue"), meta: { title: "项目管理", permission: "qa.read" } },
    { path: "/test-cases", component: () => import("@/views/TestCasesView.vue"), meta: { title: "测试用例", permission: "qa.read" } },
    { path: "/test-suites", component: () => import("@/views/TestSuitesView.vue"), meta: { title: "用例套件与归档", permission: "qa.read" } },
    { path: "/test-plans", component: () => import("@/views/PlansView.vue"), meta: { title: "计划与执行", permission: "qa.read" } },
    { path: "/defects", component: () => import("@/views/DefectsView.vue"), meta: { title: "缺陷与审计", permission: "qa.read" } },
    { path: "/quality", component: () => import("@/views/QualityReportsView.vue"), meta: { title: "质量报表", permission: "reports.read" } },
    { path: "/data-transfer", component: () => import("@/views/DataTransferView.vue"), meta: { title: "批量导入导出", permission: "imports.manage" } },
    { path: "/pipelines", component: () => import("@/views/PipelinesView.vue"), meta: { title: "CI/CD 流水线", permission: "pipeline.read" } },
    { path: "/integrations", component: () => import("@/views/IntegrationsView.vue"), meta: { title: "CI Provider 集成", permission: "integrations.read" } },
    { path: "/automation", component: () => import("@/views/AutomationView.vue"), meta: { title: "自动化运行中心", permissions: ["devices.read", "schedules.read"] } },
    { path: "/access", component: () => import("@/views/UserAccessView.vue"), meta: { title: "用户与权限", permission: "users.read" } },
    { path: "/:pathMatch(.*)*", component: () => import("@/views/NotFoundView.vue"), meta: { title: "页面不存在" } },
  ],
});

router.beforeEach(async (to) => {
  const auth = useAuthSession();
  try {
    await auth.initialize();
  } catch {
    if (to.path !== "/login") {
      return { path: "/login", query: { redirect: to.fullPath } };
    }
    return true;
  }

  if (to.meta.public) {
    if (to.path === "/login" && auth.isAuthenticated.value) return "/";
    return true;
  }
  if (!auth.isAuthenticated.value) {
    return { path: "/login", query: { redirect: to.fullPath } };
  }
  const permission = typeof to.meta.permission === "string" ? to.meta.permission : undefined;
  if (permission && !auth.can(permission) && to.path !== "/forbidden") {
    return "/forbidden";
  }
  const permissions = Array.isArray(to.meta.permissions)
    ? to.meta.permissions.filter((item): item is string => typeof item === "string")
    : [];
  if (permissions.length && !auth.canAny(...permissions) && to.path !== "/forbidden") {
    return "/forbidden";
  }
  return true;
});

router.afterEach((to) => {
  document.title = `${String(to.meta.title ?? "QA Platform")} · QA Platform Learning`;
});

export default router;
