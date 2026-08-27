<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import PageHeader from "@/components/PageHeader.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { ApiError, qaApi } from "@/api";
import type {
  ImportCommitResult,
  ImportPreview,
  Project,
  TransferEntity,
  TransferFormat,
} from "@/api";

const projects = ref<Project[]>([]);
const projectId = ref("");
const entity = ref<TransferEntity>("test-cases");
const format = ref<TransferFormat>("xlsx");
const selectedFile = ref<File | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const preview = ref<ImportPreview | null>(null);
const result = ref<ImportCommitResult | null>(null);
const loading = ref(false);
const message = ref("");
const errorMessage = ref("");
const allowPartial = ref(false);
let contextVersion = 0;
let requestVersion = 0;

const selectedProject = computed(() =>
  projects.value.find((project) => project.id === projectId.value) ?? null,
);
const canImportProject = computed(() => selectedProject.value?.status === "active");
const canCommit = computed(() => {
  if (!canImportProject.value || result.value || !preview.value || !selectedFile.value) {
    return false;
  }
  return preview.value.can_commit_clean || (allowPartial.value && preview.value.can_commit_partial);
});

onMounted(async () => {
  try {
    projects.value = await qaApi.listProjects();
    projectId.value =
      projects.value.find((project) => project.status === "active")?.id ??
      projects.value[0]?.id ??
      "";
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : "项目加载失败";
  }
});

watch([projectId, entity], () => clearPreview(), { flush: "sync" });
watch(format, () => {
  selectedFile.value = null;
  if (fileInput.value) fileInput.value.value = "";
  clearPreview();
}, { flush: "sync" });

function clearPreview() {
  contextVersion += 1;
  preview.value = null;
  result.value = null;
  message.value = "";
  allowPartial.value = false;
}

function pickFile(event: Event) {
  const input = event.target as HTMLInputElement;
  selectedFile.value = input.files?.[0] ?? null;
  clearPreview();
}

function saveDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

async function downloadTemplate() {
  loading.value = true;
  errorMessage.value = "";
  try {
    const download = await qaApi.downloadTransferTemplate(entity.value, format.value);
    saveDownload(download.blob, download.filename ?? `${entity.value}-template.${format.value}`);
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : "模板下载失败";
  } finally {
    loading.value = false;
  }
}

async function exportCurrent() {
  if (!projectId.value) return;
  loading.value = true;
  errorMessage.value = "";
  try {
    const download = await qaApi.exportData(entity.value, projectId.value, format.value);
    saveDownload(download.blob, download.filename ?? `${entity.value}-export.${format.value}`);
    message.value = `已导出 ${download.headers.get("X-Export-Count") ?? "当前"} 条记录`;
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : "导出失败";
  } finally {
    loading.value = false;
  }
}

async function previewFile() {
  if (loading.value || !selectedFile.value || !projectId.value) return;
  const requestId = ++requestVersion;
  const contextId = contextVersion;
  const selectedProject = projectId.value;
  const selectedEntity = entity.value;
  const file = selectedFile.value;
  loading.value = true;
  errorMessage.value = "";
  result.value = null;
  try {
    const next = await qaApi.previewImport(selectedEntity, selectedProject, file);
    if (
      requestId === requestVersion &&
      contextId === contextVersion &&
      selectedProject === projectId.value &&
      selectedEntity === entity.value &&
      file === selectedFile.value
    ) preview.value = next;
  } catch (error) {
    if (requestId === requestVersion && contextId === contextVersion) {
      errorMessage.value = error instanceof ApiError ? error.message : "导入预检失败";
    }
  } finally {
    if (requestId === requestVersion) loading.value = false;
  }
}

async function commitFile() {
  if (loading.value || !selectedFile.value || !projectId.value || !preview.value || !canCommit.value) return;
  if (allowPartial.value && preview.value.invalid_rows && !window.confirm("部分导入不是原子事务：有效行会创建，无效行会跳过。确认继续？")) return;
  const requestId = ++requestVersion;
  const contextId = contextVersion;
  const selectedProject = projectId.value;
  const selectedEntity = entity.value;
  const file = selectedFile.value;
  const expectedSha256 = preview.value.sha256;
  loading.value = true;
  errorMessage.value = "";
  try {
    const next = await qaApi.commitImport(
      selectedEntity,
      selectedProject,
      file,
      expectedSha256,
      !allowPartial.value,
    );
    if (
      requestId === requestVersion &&
      contextId === contextVersion &&
      selectedProject === projectId.value &&
      selectedEntity === entity.value &&
      file === selectedFile.value
    ) {
      result.value = next;
      message.value = `导入结束：创建 ${next.created_rows}，失败 ${next.failed_rows}，跳过 ${next.skipped_rows}`;
    }
  } catch (error) {
    if (requestId === requestVersion && contextId === contextVersion) {
      errorMessage.value = error instanceof ApiError ? error.message : "提交导入失败";
    }
  } finally {
    if (requestId === requestVersion) loading.value = false;
  }
}
</script>

<template>
  <section>
    <PageHeader
      eyebrow="DATA TRANSFER"
      title="Excel / CSV 批量导入导出"
      description="先下载版本化模板，再执行只读预检。首版只支持 create-only；缺陷统一以 open 创建，不能绕过状态机。"
    >
      <template #actions>
        <button class="button" :disabled="loading" @click="downloadTemplate">下载空模板</button>
        <button class="button primary" :disabled="loading || !projectId" @click="exportCurrent">导出项目数据</button>
      </template>
    </PageHeader>

    <div class="notice">
      <b>事务边界说明</b>
      <span>当前 Repository 尚未统一 Unit of Work，因此仅“无错误预检”适合作为默认提交；勾选部分导入时，成功行不会因后续失败自动回滚。</span>
    </div>
    <div v-if="message" class="notice online">{{ message }}</div>
    <div v-if="errorMessage" class="notice">{{ errorMessage }}</div>

    <div class="transfer-grid">
      <article class="panel controls">
        <div class="panel-title"><div><small>INPUT</small><h2>选择范围与文件</h2></div></div>
        <label>目标项目<select v-model="projectId" :disabled="loading"><option value="" disabled>请选择项目</option><option v-for="project in projects" :key="project.id" :value="project.id">{{ project.key }} · {{ project.name }}{{ project.status === 'archived' ? '（已归档，仅导出）' : '' }}</option></select></label>
        <label>数据类型<select v-model="entity" :disabled="loading"><option value="test-cases">测试用例</option><option value="defects">缺陷</option></select></label>
        <label>模板 / 导出格式<select v-model="format" :disabled="loading"><option value="xlsx">Excel .xlsx</option><option value="csv">CSV UTF-8</option></select></label>
        <p v-if="selectedProject?.status === 'archived'" class="empty compact">归档项目仍可导出，但不再接受导入。</p>
        <label class="file-picker">待导入文件<input ref="fileInput" :accept="format === 'xlsx' ? '.xlsx' : '.csv'" type="file" :disabled="loading || !canImportProject" @change="pickFile" /><span>{{ selectedFile?.name ?? "选择 .xlsx 或 .csv" }}</span></label>
        <button class="button primary" :disabled="loading || !selectedFile || !projectId || !canImportProject" @click="previewFile">{{ loading ? "处理中…" : "只读预检" }}</button>
      </article>

      <article class="panel preview-panel">
        <div class="panel-title"><div><small>PREVIEW</small><h2>校验结果</h2></div><StatusBadge v-if="preview" :status="preview.invalid_rows ? 'failed' : 'succeeded'" /></div>
        <div v-if="preview" class="preview-stats">
          <div><span>总行数</span><b>{{ preview.total_rows }}</b></div><div><span>有效</span><b>{{ preview.valid_rows }}</b></div><div><span>无效</span><b>{{ preview.invalid_rows }}</b></div><div><span>警告</span><b>{{ preview.warning_count }}</b></div>
        </div>
        <p v-if="!preview" class="empty">选择文件并执行预检；预检不会写入数据库。</p>
        <template v-else>
          <div class="hash"><span>SHA-256</span><code>{{ preview.sha256 }}</code><small>模板版本 {{ preview.template_version }}</small></div>
          <div v-if="preview.issues.length" class="issue-list">
            <article v-for="(issue,index) in preview.issues" :key="`${issue.sheet}-${issue.row}-${index}`" :class="issue.severity">
              <b>{{ issue.sheet }} · 第 {{ issue.row }} 行 · {{ issue.field || "整行" }}</b><span>{{ issue.message }}</span><code>{{ issue.code }}</code>
            </article>
            <small v-if="preview.omitted_issue_count">另有 {{ preview.omitted_issue_count }} 条问题未在页面展开</small>
          </div>
          <label v-if="preview.can_commit_partial && !preview.can_commit_clean" class="partial"><input v-model="allowPartial" type="checkbox" /> 我理解部分导入不是原子事务，允许跳过无效行</label>
          <button class="button primary" :disabled="loading || !canCommit" @click="commitFile">提交创建</button>
        </template>
      </article>
    </div>

    <div v-if="result" class="table-wrap result-table">
      <table><thead><tr><th>位置</th><th>行标识</th><th>状态</th><th>新记录</th><th>说明</th></tr></thead><tbody><tr v-for="row in result.rows" :key="`${row.sheet}-${row.row}`"><td>{{ row.sheet }} / {{ row.row }}</td><td><code>{{ row.row_key }}</code></td><td><StatusBadge :status="row.status" /></td><td><code>{{ row.entity_id ?? "—" }}</code></td><td>{{ row.issues.map(issue=>issue.message).join("；") || "已创建" }}</td></tr></tbody></table>
    </div>
  </section>
</template>

<style scoped>
.transfer-grid{display:grid;grid-template-columns:340px minmax(0,1fr);gap:16px;align-items:start}.controls{display:grid;gap:14px}.controls label{display:grid;gap:6px;color:#65736d;font-size:9px;font-weight:700}.controls select,.controls input[type=file]{width:100%;height:40px;padding:0 10px;border:1px solid #d9e3de;border-radius:8px;background:#fff}.file-picker input{position:absolute;opacity:0;pointer-events:none}.file-picker span{display:flex;align-items:center;height:42px;padding:0 11px;border:1px dashed #afc4ba;border-radius:8px;color:var(--green);background:#f3faf6}.preview-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}.preview-stats div{padding:12px;border-radius:9px;background:#f5f8f6}.preview-stats span,.preview-stats b{display:block}.preview-stats span{color:var(--muted);font-size:8px}.preview-stats b{margin-top:6px;font-size:18px}.hash{display:grid;gap:5px;margin:15px 0;padding:11px;border-radius:8px;background:#f4f7f5}.hash span,.hash small{color:var(--muted);font-size:8px}.hash code{overflow:hidden;text-overflow:ellipsis;font-size:8px}.issue-list{display:grid;gap:7px;max-height:300px;overflow:auto;margin-bottom:14px}.issue-list article{display:grid;grid-template-columns:1fr auto;gap:4px;padding:9px;border-left:3px solid #d39a45;background:#fff8ec;font-size:9px}.issue-list article.error{border-color:#c45151;background:#fff1f1}.issue-list article span{grid-column:1/-1;color:#66736e}.issue-list code{font-size:7px}.partial{display:flex;align-items:flex-start;gap:7px;margin:10px 0;color:#9b6921;font-size:9px;line-height:1.5}.result-table{margin-top:17px}@media(max-width:900px){.transfer-grid{grid-template-columns:1fr}}@media(max-width:600px){.preview-stats{grid-template-columns:repeat(2,1fr)}}
</style>
