<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import { API_BASE_LABEL } from "@/api";
import { useAuthSession } from "@/auth/session";

const route = useRoute();
const auth = useAuthSession();
const title = computed(() => String(route.meta.title ?? "QA Platform"));
const nav: Array<{ to: string; label: string; icon: string; permissions: string[] }> = [
  { to: "/", label: "仪表盘", icon: "概", permissions: ["qa.read"] },
  { to: "/projects", label: "项目", icon: "项", permissions: ["qa.read"] },
  { to: "/test-cases", label: "测试用例", icon: "例", permissions: ["qa.read"] },
  { to: "/test-suites", label: "套件与归档", icon: "套", permissions: ["qa.read"] },
  { to: "/test-plans", label: "计划与执行", icon: "执", permissions: ["qa.read"] },
  { to: "/defects", label: "缺陷与审计", icon: "缺", permissions: ["qa.read"] },
  { to: "/quality", label: "质量报表", icon: "质", permissions: ["reports.read"] },
  { to: "/data-transfer", label: "导入导出", icon: "表", permissions: ["imports.manage"] },
  { to: "/pipelines", label: "CI/CD 流水线", icon: "流", permissions: ["pipeline.read"] },
  { to: "/integrations", label: "Provider 集成", icon: "接", permissions: ["integrations.read"] },
  { to: "/automation", label: "自动化运行中心", icon: "自", permissions: ["devices.read", "schedules.read"] },
  { to: "/access", label: "用户与权限", icon: "权", permissions: ["users.read"] },
];
const visibleNav = computed(() => nav.filter((item) => auth.canAny(...item.permissions)));

async function logout() {
  try {
    await auth.logout();
  } finally {
    window.location.assign("/login");
  }
}
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <RouterLink class="brand" to="/"><b>Q</b><span><strong>QA Forge</strong><small>Learning Platform</small></span></RouterLink>
      <nav><RouterLink v-for="item in visibleNav" :key="item.to" :to="item.to"><i>{{ item.icon }}</i>{{ item.label }}</RouterLink></nav>
      <footer><span>● 本地教学环境</span><small>local_lab 默认硬拒绝所有网络 Provider</small></footer>
    </aside>
    <div class="main-column">
      <header class="topbar"><div><small>WORKSPACE</small><strong>{{ title }}</strong></div><div class="topbar-meta"><span>LOCAL SQLITE</span><code :title="API_BASE_LABEL">API · {{ API_BASE_LABEL }}</code><div v-if="auth.state.user" class="user-menu"><b>{{ auth.state.user.display_name }}</b><small>@{{ auth.state.user.username }}</small><button @click="logout">退出</button></div></div></header>
      <main><RouterView /></main>
    </div>
  </div>
</template>
