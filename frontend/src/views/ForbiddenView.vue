<template>
  <section class="not-found">
    <strong>403</strong>
    <h1>当前角色没有此权限</h1>
    <p>页面显示不是安全边界，后端也会对每个请求重新校验权限。</p>
    <RouterLink class="button primary" to="/">返回仪表盘</RouterLink>
  </section>
</template>
