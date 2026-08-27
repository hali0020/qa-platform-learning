<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import PageHeader from "@/components/PageHeader.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { ApiError, qaApi } from "@/api";
import type { CountAndRate, Project, QualityReport } from "@/api";

const projects = ref<Project[]>([]);
const projectId = ref("");
const report = ref<QualityReport | null>(null);
const loading = ref(false);
const errorMessage = ref("");
const granularity = ref<"day" | "week">("day");
const today = new Date();
const start = new Date(today);
start.setDate(start.getDate() - 29);
const dateFrom = ref(localDate(start));
const dateTo = ref(localDate(today));
let filterVersion = 0;
let requestVersion = 0;

watch([projectId, dateFrom, dateTo, granularity], () => {
  filterVersion += 1;
  report.value = null;
}, { flush: "sync" });

function localDate(value: Date) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

const trendMax = computed(() => Math.max(
  1,
  ...(report.value?.trends.map((point) =>
    point.passed + point.failed + point.blocked + point.skipped + point.not_run,
  ) ?? [1]),
));

onMounted(async () => {
  try {
    projects.value = await qaApi.listProjects();
    projectId.value = projects.value[0]?.id ?? "";
    if (projectId.value) await loadReport();
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : "质量报表加载失败";
  }
});

async function loadReport() {
  if (loading.value || !projectId.value) return;
  const requestId = ++requestVersion;
  const selectedFilterVersion = filterVersion;
  const query = {
    project_id: projectId.value,
    date_from: dateFrom.value,
    date_to: dateTo.value,
    granularity: granularity.value,
    timezone: "Asia/Shanghai",
  } as const;
  loading.value = true;
  errorMessage.value = "";
  try {
    const next = await qaApi.getQualityReport(query);
    if (requestId === requestVersion && selectedFilterVersion === filterVersion) {
      report.value = next;
    }
  } catch (error) {
    if (requestId === requestVersion && selectedFilterVersion === filterVersion) {
      errorMessage.value = error instanceof ApiError ? error.message : "质量报表加载失败";
    }
  } finally {
    if (requestId === requestVersion) loading.value = false;
  }
}

function rateText(rate: CountAndRate) {
  return rate.percent === null ? "—" : `${rate.percent.toFixed(1)}%`;
}

function barHeight(value: number) {
  return `${Math.max(value ? 4 : 0, (value / trendMax.value) * 100)}%`;
}
</script>

<template>
  <section>
    <PageHeader
      eyebrow="QUALITY ANALYTICS"
      title="质量报表、趋势与覆盖率"
      description="指标由后端统一计算并公开分子、分母。空分母显示为“—”，不会伪造 0%；趋势只使用已完成执行的最终结果。"
    >
      <template #actions>
        <button class="button primary" :disabled="loading || !projectId" @click="loadReport">{{ loading ? "计算中…" : "重新计算" }}</button>
      </template>
    </PageHeader>

    <div class="panel filters">
      <label>项目<select v-model="projectId" :disabled="loading"><option v-for="project in projects" :key="project.id" :value="project.id">{{ project.key }} · {{ project.name }}</option></select></label>
      <label>开始日期<input v-model="dateFrom" type="date" :disabled="loading" /></label>
      <label>结束日期<input v-model="dateTo" type="date" :disabled="loading" /></label>
      <label>趋势粒度<select v-model="granularity" :disabled="loading"><option value="day">按天</option><option value="week">按周</option></select></label>
    </div>
    <div v-if="errorMessage" class="notice">{{ errorMessage }}</div>

    <template v-if="report">
      <div class="stats metric-grid">
        <article title="当前 active 且 automated 的用例数 / 当前 active 用例数"><span>自动化覆盖率</span><strong>{{ rateText(report.summary.test_cases.automation_coverage) }}</strong><small>{{ report.summary.test_cases.automation_coverage.numerator }} / {{ report.summary.test_cases.automation_coverage.denominator }} 个当前启用用例</small></article>
        <article title="期间已执行的不同用例 / 当前 active 用例数"><span>执行触达率</span><strong>{{ rateText(report.summary.test_cases.execution_reach) }}</strong><small>{{ report.summary.test_cases.execution_reach.numerator }} / {{ report.summary.test_cases.execution_reach.denominator }} 个用例被执行</small></article>
        <article title="passed / (passed + failed)，blocked、skipped、not_run 不进入分母"><span>执行通过率</span><strong>{{ rateText(report.summary.executions.pass_rate) }}</strong><small>{{ report.summary.executions.pass_rate.numerator }} / {{ report.summary.executions.pass_rate.denominator }} 个有效判定结果</small></article>
        <article title="有关联缺陷的 failed/blocked 结果对 / 全部 failed/blocked 结果对"><span>失败提单覆盖</span><strong>{{ rateText(report.summary.executions.failure_defect_coverage) }}</strong><small>{{ report.summary.executions.failure_defect_coverage.numerator }} / {{ report.summary.executions.failure_defect_coverage.denominator }} 个失败结果已提单</small></article>
      </div>

      <div class="analytics-grid">
        <article class="panel trend-panel">
          <div class="panel-title"><div><small>TREND</small><h2>执行结果趋势</h2></div><span>{{ report.summary.period.date_from }} → {{ report.summary.period.date_to }}</span></div>
          <div class="chart" :class="{ weekly: report.granularity === 'week' }">
            <div v-for="point in report.trends" :key="point.bucket_start" class="bar-group" :title="`${point.bucket_start} · 通过 ${point.passed} · 失败 ${point.failed} · 阻塞 ${point.blocked}`">
              <div class="bar-stack">
                <i class="passed" :style="{height:barHeight(point.passed)}"></i>
                <i class="failed" :style="{height:barHeight(point.failed)}"></i>
                <i class="blocked" :style="{height:barHeight(point.blocked)}"></i>
              </div>
              <small>{{ point.bucket_start.slice(5) }}</small>
            </div>
          </div>
          <footer><span><i class="passed"></i>通过</span><span><i class="failed"></i>失败</span><span><i class="blocked"></i>阻塞</span></footer>
        </article>

        <article class="panel defect-summary">
          <div class="panel-title"><div><small>DEFECTS</small><h2>缺陷健康度</h2></div><StatusBadge :status="report.summary.defects.high_severity_not_closed_current ? 'failed' : 'succeeded'" /></div>
          <dl><div><dt>期间新增</dt><dd>{{ report.summary.defects.created_in_period }}</dd></div><div><dt>期间解决</dt><dd>{{ report.summary.defects.resolved_in_period }}</dd></div><div><dt>期间关闭</dt><dd>{{ report.summary.defects.closed_in_period }}</dd></div><div><dt>期间重开</dt><dd>{{ report.summary.defects.reopened_in_period }}</dd></div><div><dt>当前未关闭</dt><dd>{{ report.summary.defects.not_closed_current }}</dd></div><div><dt>高严重未关闭</dt><dd>{{ report.summary.defects.high_severity_not_closed_current }}</dd></div></dl>
        </article>
      </div>

      <div class="table-wrap coverage-table">
        <table><thead><tr><th>套件路径</th><th>状态</th><th>启用用例</th><th>自动化覆盖</th><th>执行触达</th><th>失败提单覆盖</th></tr></thead><tbody><tr v-for="item in report.coverage_by_suite" :key="item.suite_id ?? 'unassigned'"><td><b>{{ item.suite_path || "未分配套件" }}</b></td><td><StatusBadge :status="item.suite_status ?? 'active'" /></td><td>{{ item.active_cases }}</td><td>{{ rateText(item.automation_coverage) }} <small>{{ item.automated_cases }}/{{ item.active_cases }}</small></td><td>{{ rateText(item.execution_reach) }} <small>{{ item.executed_cases }}/{{ item.active_cases }}</small></td><td>{{ rateText(item.failure_defect_coverage) }} <small>{{ item.linked_failed_or_blocked_results }}/{{ item.failed_or_blocked_results }}</small></td></tr></tbody></table>
      </div>
    </template>
    <div v-else-if="!loading" class="panel empty">选择项目和日期范围后生成质量报表。</div>
  </section>
</template>

<style scoped>
.filters{display:grid;grid-template-columns:2fr repeat(3,1fr);gap:12px;margin-bottom:17px}.filters label{display:grid;gap:5px;color:var(--muted);font-size:8px;font-weight:700}.filters select,.filters input{height:38px;padding:0 9px;border:1px solid #dbe4e0;border-radius:8px;background:#fff}.metric-grid article{min-width:0}.analytics-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(260px,.75fr);gap:15px}.panel-title>span{color:var(--muted);font-size:8px}.chart{height:250px;display:flex;align-items:end;gap:5px;padding:22px 0 4px;border-bottom:1px solid #dce5e0;overflow:auto}.bar-group{min-width:16px;height:100%;display:grid;grid-template-rows:1fr auto;gap:6px;align-items:end}.weekly .bar-group{min-width:42px}.bar-stack{height:100%;display:flex;align-items:end;gap:1px}.bar-stack i{display:block;min-width:4px;flex:1;border-radius:3px 3px 0 0}.passed{background:#42bd84}.failed{background:#d55c5c}.blocked{background:#d49a42}.bar-group small{display:block;transform:rotate(-50deg);transform-origin:top left;color:#87938e;font-size:6px;white-space:nowrap}.trend-panel footer{display:flex;gap:14px;margin-top:20px}.trend-panel footer span{display:flex;align-items:center;gap:5px;color:var(--muted);font-size:8px}.trend-panel footer i{width:7px;height:7px;border-radius:2px}.defect-summary dl{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin:0}.defect-summary dl div{padding:13px;border-radius:9px;background:#f5f8f6}.defect-summary dt{color:var(--muted);font-size:8px}.defect-summary dd{margin:7px 0 0;font-size:20px;font-weight:800}.coverage-table{margin-top:16px}.coverage-table small{display:block;margin-top:3px;color:var(--muted);font-size:7px}@media(max-width:1000px){.filters{grid-template-columns:repeat(2,1fr)}.analytics-grid{grid-template-columns:1fr}}@media(max-width:600px){.filters{grid-template-columns:1fr}}
</style>
