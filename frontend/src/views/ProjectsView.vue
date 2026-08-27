<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { qaApi } from "@/api";
import type { Project, ProjectStatus } from "@/api";
import PageHeader from "@/components/PageHeader.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { useAuthSession } from "@/auth/session";

const auth = useAuthSession();
const canWrite = computed(() => auth.can("qa.write"));

const projects = ref<Project[]>([]);
const query = ref("");
const loading = ref(true);
const loadFailed = ref(false);
const saving = ref(false);
const busyProjectId = ref<string | null>(null);
const errorMessage = ref("");
const successMessage = ref("");
const formOpen = ref(false);
const editingProjectId = ref<string | null>(null);
const form = reactive({ key: "", name: "", description: "" });
const operationInProgress = computed(
  () => saving.value || busyProjectId.value !== null,
);

const visible = computed(() => {
  const keyword = query.value.trim().toLowerCase();
  if (!keyword) return projects.value;
  return projects.value.filter((project) =>
    `${project.name} ${project.key} ${project.description}`.toLowerCase().includes(keyword),
  );
});

const formTitle = computed(() => (editingProjectId.value ? "编辑项目" : "创建项目"));
const isEditing = computed(() => editingProjectId.value !== null);
const dateLabel = (value: string) => new Date(value).toLocaleDateString("zh-CN");

function resetMessages() {
  errorMessage.value = "";
  successMessage.value = "";
}

function readableError(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败，请稍后重试";
}

function replaceProject(project: Project) {
  const exists = projects.value.some((item) => item.id === project.id);
  projects.value = exists
    ? projects.value.map((item) => (item.id === project.id ? project : item))
    : [...projects.value, project];
}

async function loadProjects() {
  loading.value = true;
  loadFailed.value = false;
  resetMessages();
  try {
    projects.value = await qaApi.listProjects();
  } catch (error) {
    loadFailed.value = true;
    errorMessage.value = readableError(error);
  } finally {
    loading.value = false;
  }
}

function openCreateForm() {
  if (!canWrite.value || operationInProgress.value) return;
  resetMessages();
  editingProjectId.value = null;
  form.key = "";
  form.name = "";
  form.description = "";
  formOpen.value = true;
}

function openEditForm(project: Project) {
  if (!canWrite.value || operationInProgress.value) return;
  resetMessages();
  editingProjectId.value = project.id;
  form.key = project.key;
  form.name = project.name;
  form.description = project.description;
  formOpen.value = true;
}

function closeForm() {
  formOpen.value = false;
  editingProjectId.value = null;
}

async function submitForm() {
  if (!canWrite.value) return;
  if (operationInProgress.value) return;
  resetMessages();
  const key = form.key.trim().toUpperCase();
  const name = form.name.trim();
  const description = form.description.trim();

  if (!name) {
    errorMessage.value = "项目名称不能为空";
    return;
  }
  if (!isEditing.value && !/^[A-Z][A-Z0-9_-]{1,19}$/.test(key)) {
    errorMessage.value = "项目代号需以字母开头，只能包含字母、数字、下划线或连字符（2–20 位）";
    return;
  }

  saving.value = true;
  try {
    if (editingProjectId.value) {
      const updated = await qaApi.updateProject(editingProjectId.value, { name, description });
      replaceProject(updated);
      successMessage.value = `项目「${updated.name}」已保存到本机数据库`;
    } else {
      const created = await qaApi.createProject({ key, name, description });
      replaceProject(created);
      successMessage.value = `项目「${created.name}」已写入本机数据库`;
    }
    closeForm();
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    saving.value = false;
  }
}

async function changeStatus(project: Project, status: ProjectStatus) {
  if (!canWrite.value || operationInProgress.value) return;
  resetMessages();
  busyProjectId.value = project.id;
  try {
    const updated = await qaApi.transitionProject(project.id, { status });
    replaceProject(updated);
    successMessage.value =
      status === "archived"
        ? `项目「${updated.name}」已归档`
        : `项目「${updated.name}」已恢复为进行中`;
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    busyProjectId.value = null;
  }
}

async function deleteProject(project: Project) {
  if (!canWrite.value || operationInProgress.value) return;
  const confirmed = window.confirm(
    `确认永久删除项目「${project.name}（${project.key}）」吗？\n\n这会删除本机数据库中的项目记录，且无法撤销；有关联用例或计划时后端会拒绝删除。`,
  );
  if (!confirmed) return;

  resetMessages();
  busyProjectId.value = project.id;
  try {
    await qaApi.deleteProject(project.id);
    projects.value = projects.value.filter((item) => item.id !== project.id);
    if (editingProjectId.value === project.id) closeForm();
    successMessage.value = `项目「${project.name}」已从本机数据库删除`;
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    busyProjectId.value = null;
  }
}

onMounted(loadProjects);
</script>

<template>
  <section>
    <PageHeader
      eyebrow="PROJECTS"
      title="项目管理"
      description="项目是成员、版本、测试资产、执行记录和流水线配置的业务边界。"
    >
      <template #actions>
        <button v-if="canWrite" class="button primary" :disabled="loading || operationInProgress" @click="openCreateForm">
          ＋ 新建项目
        </button>
      </template>
    </PageHeader>

    <div class="notice online">
      <b>已接入本机 Project API</b>
      <span>页面操作通过 FastAPI 读写本机 SQLite，刷新或重启服务后数据仍会保留。</span>
    </div>

    <div v-if="errorMessage" class="notice error" role="alert">
      <b>操作未完成</b>
      <span>{{ errorMessage }}</span>
    </div>
    <div v-if="successMessage" class="notice success" role="status">
      <b>操作成功</b>
      <span>{{ successMessage }}</span>
    </div>

    <form v-if="canWrite && formOpen" class="panel project-form" @submit.prevent="submitForm">
      <div class="form-heading">
        <div>
          <small>LOCAL DATABASE</small>
          <h2>{{ formTitle }}</h2>
        </div>
        <button
          class="close-button"
          type="button"
          :disabled="operationInProgress"
          aria-label="关闭项目表单"
          @click="closeForm"
        >
          ×
        </button>
      </div>
      <div class="form-grid">
        <label>
          <span>项目代号</span>
          <input
            v-model="form.key"
            :disabled="isEditing || operationInProgress"
            autocomplete="off"
            maxlength="20"
            minlength="2"
            pattern="[A-Za-z][A-Za-z0-9_-]{1,19}"
            placeholder="例如 GAME_QA"
            required
          />
          <small>{{ isEditing ? "项目创建后代号不可修改" : "以字母开头，2–20 位；保存时自动转为大写" }}</small>
        </label>
        <label>
          <span>项目名称</span>
          <input
            v-model="form.name"
            :disabled="operationInProgress"
            maxlength="100"
            placeholder="例如 新版本回归测试"
            required
          />
          <small>用于页面展示和测试资产归属</small>
        </label>
        <label class="description-field">
          <span>项目描述</span>
          <textarea
            v-model="form.description"
            :disabled="operationInProgress"
            maxlength="500"
            rows="3"
            placeholder="说明测试范围、目标或版本背景"
          ></textarea>
          <small>{{ form.description.length }}/500</small>
        </label>
      </div>
      <div class="form-actions">
        <button class="button" type="button" :disabled="operationInProgress" @click="closeForm">取消</button>
        <button class="button primary" type="submit" :disabled="operationInProgress">
          {{ saving ? "正在保存…" : isEditing ? "保存修改" : "创建并写入数据库" }}
        </button>
      </div>
    </form>

    <div class="toolbar">
      <input v-model="query" class="search" type="search" placeholder="搜索项目名称、代号或描述" />
      <span v-if="!loading && !loadFailed" class="project-count">显示 {{ visible.length }} / {{ projects.length }} 个项目</span>
    </div>

    <div v-if="loading" class="panel state-panel" role="status">
      <span class="spinner" aria-hidden="true"></span>
      <b>正在从本机数据库加载项目…</b>
    </div>

    <div v-else-if="loadFailed" class="panel state-panel empty-state">
      <b>项目列表加载失败</b>
      <span>请确认本机后端已经启动，然后重新加载。</span>
      <button class="button primary" @click="loadProjects">重新加载</button>
    </div>

    <template v-else>
      <div v-if="visible.length" class="card-grid">
        <article v-for="project in visible" :key="project.id" class="card project-card">
          <header>
            <code>{{ project.key }}</code>
            <StatusBadge
              :status="project.status"
              :label="project.status === 'active' ? '进行中' : '已归档'"
            />
          </header>
          <h2>{{ project.name }}</h2>
          <p>{{ project.description || "暂未填写项目描述。" }}</p>
          <dl>
            <div><dt>创建日期</dt><dd>{{ dateLabel(project.created_at) }}</dd></div>
            <div><dt>最近更新</dt><dd>{{ dateLabel(project.updated_at) }}</dd></div>
          </dl>
          <div class="card-actions">
            <RouterLink to="/test-cases">查看测试资产 →</RouterLink>
            <button v-if="canWrite" class="text-button" :disabled="operationInProgress" @click="openEditForm(project)">
              编辑
            </button>
            <button
              v-if="canWrite"
              class="text-button"
              :disabled="operationInProgress"
              @click="changeStatus(project, project.status === 'active' ? 'archived' : 'active')"
            >
              {{ busyProjectId === project.id ? "处理中…" : project.status === "active" ? "归档" : "恢复" }}
            </button>
            <button
              v-if="canWrite"
              class="text-button danger"
              :disabled="operationInProgress"
              @click="deleteProject(project)"
            >
              删除
            </button>
          </div>
        </article>
      </div>

      <div v-else class="panel state-panel empty-state">
        <b>{{ projects.length ? "没有符合条件的项目" : "本机数据库中还没有项目" }}</b>
        <span>{{ projects.length ? "换个关键词再搜索一次。" : "创建第一个项目，开始建立测试资产。" }}</span>
        <button
          v-if="canWrite && !projects.length"
          class="button primary"
          :disabled="operationInProgress"
          @click="openCreateForm"
        >
          ＋ 创建第一个项目
        </button>
      </div>
    </template>
  </section>
</template>

<style scoped>
.project-form {
  margin-bottom: 18px;
  border-color: #cde6da;
}

.form-heading,
.form-actions,
.toolbar,
.card-actions {
  display: flex;
  align-items: center;
}

.form-heading {
  justify-content: space-between;
  margin-bottom: 18px;
}

.form-heading small {
  color: var(--green);
  font-size: 8px;
  font-weight: 700;
  letter-spacing: 0.15em;
}

.form-heading h2 {
  margin-top: 5px;
}

.close-button {
  width: 32px;
  height: 32px;
  border: 0;
  border-radius: 8px;
  color: #6d7b75;
  background: #f1f5f3;
  font-size: 20px;
}

.form-grid {
  display: grid;
  grid-template-columns: minmax(180px, 0.45fr) minmax(240px, 1fr);
  gap: 16px;
}

.form-grid label {
  display: grid;
  gap: 7px;
}

.form-grid label > span {
  color: #34463f;
  font-size: 10px;
  font-weight: 700;
}

.form-grid input,
.form-grid textarea {
  width: 100%;
  border: 1px solid #d9e3de;
  border-radius: 8px;
  outline: none;
  color: #25352f;
  background: #fff;
  font: inherit;
  font-size: 11px;
}

.form-grid input {
  height: 40px;
  padding: 0 12px;
}

.form-grid textarea {
  padding: 11px 12px;
  resize: vertical;
}

.form-grid input:focus,
.form-grid textarea:focus {
  border-color: var(--green);
  box-shadow: 0 0 0 3px rgba(32, 134, 94, 0.1);
}

.form-grid input:disabled {
  color: #77857f;
  background: #f2f5f3;
}

.form-grid label > small {
  color: var(--muted);
  font-size: 8px;
}

.description-field {
  grid-column: 1 / -1;
}

.description-field > small {
  justify-self: end;
}

.form-actions {
  justify-content: flex-end;
  gap: 8px;
  margin-top: 18px;
  padding-top: 16px;
  border-top: 1px solid #edf1ef;
}

.toolbar {
  justify-content: space-between;
  gap: 16px;
}

.toolbar .search {
  margin-bottom: 17px;
}

.project-count {
  margin-bottom: 17px;
  color: var(--muted);
  font-size: 9px;
}

.notice.error {
  border-color: #efcece;
  color: #a43f3f;
  background: #fff3f3;
}

.notice.success {
  border-color: #cde6da;
  color: #236f50;
  background: #eff9f4;
}

.project-card {
  display: flex;
  min-width: 0;
  flex-direction: column;
}

.project-card > p {
  overflow-wrap: anywhere;
}

.project-card dl {
  margin-top: auto;
}

.card-actions {
  flex-wrap: wrap;
  gap: 11px;
}

.card-actions > a {
  margin-right: auto;
  color: var(--green);
  font-size: 10px;
  font-weight: 700;
}

.text-button {
  padding: 2px 0;
  border: 0;
  color: #50635b;
  background: transparent;
  font-size: 9px;
  font-weight: 700;
}

.text-button:hover:not(:disabled) {
  color: var(--green);
}

.text-button.danger {
  color: #b04a4a;
}

.button:disabled,
.text-button:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.state-panel {
  display: grid;
  min-height: 190px;
  place-content: center;
  justify-items: center;
  gap: 10px;
  color: var(--muted);
  text-align: center;
}

.state-panel b {
  color: #42534c;
  font-size: 12px;
}

.state-panel span {
  font-size: 10px;
}

.empty-state .button {
  margin-top: 6px;
}

.spinner {
  width: 22px;
  height: 22px;
  border: 2px solid #dce8e2;
  border-top-color: var(--green);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

@media (max-width: 700px) {
  .notice,
  .toolbar {
    align-items: flex-start;
    flex-direction: column;
  }

  .form-grid {
    grid-template-columns: 1fr;
  }

  .description-field {
    grid-column: auto;
  }

  .project-count {
    margin-top: -10px;
  }
}
</style>
