<script setup lang="ts">
import { computed } from "vue";
import type { AuditEvent } from "@/api";

const props = withDefaults(
  defineProps<{
    events: AuditEvent[];
    loading?: boolean;
    error?: string;
    emptyText?: string;
  }>(),
  {
    loading: false,
    error: "",
    emptyText: "还没有审计记录。",
  },
);

defineEmits<{ retry: [] }>();

const actionLabels: Record<string, string> = {
  created: "创建记录",
  updated: "修改字段",
  status_changed: "变更状态",
  deleted: "删除记录",
  snapshot_created: "创建快照",
};

const fieldLabels: Record<string, string> = {
  title: "标题",
  description: "描述",
  severity: "严重程度",
  priority: "优先级",
  status: "状态",
  reporter: "报告人",
  assignee: "负责人",
  environment: "测试环境",
  reproduction_steps: "复现步骤",
  expected_result: "预期结果",
  actual_result: "实际结果",
  execution_id: "关联执行",
  case_id: "关联用例",
  resolution: "解决说明",
  parent_id: "父套件",
  name: "名称",
  position: "排序位置",
  scope_type: "归档范围类型",
  scope_id: "归档范围",
  version: "归档版本",
  case_count: "用例数量",
};

const orderedEvents = computed(() =>
  [...props.events].sort((left, right) => right.created_at.localeCompare(left.created_at)),
);

function dateLabel(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function valueLabel(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (Array.isArray(value)) return value.length ? value.map(valueLabel).join("；") : "—";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}
</script>

<template>
  <div class="audit-timeline">
    <div v-if="loading" class="audit-state" role="status">
      <span class="spinner" aria-hidden="true"></span>
      <span>正在读取审计历史…</span>
    </div>

    <div v-else-if="error" class="audit-state audit-state--error" role="alert">
      <b>审计记录加载失败</b>
      <span>{{ error }}</span>
      <button class="button" type="button" @click="$emit('retry')">重新加载</button>
    </div>

    <p v-else-if="!orderedEvents.length" class="audit-state">{{ emptyText }}</p>

    <ol v-else>
      <li v-for="event in orderedEvents" :key="event.id">
        <i aria-hidden="true"></i>
        <article>
          <header>
            <div>
              <b>{{ actionLabels[event.action] ?? event.action }}</b>
              <small>{{ event.actor || "local-user" }}</small>
            </div>
            <time :datetime="event.created_at">{{ dateLabel(event.created_at) }}</time>
          </header>

          <p v-if="event.comment" class="comment">{{ event.comment }}</p>

          <dl v-if="Object.keys(event.changes).length" class="changes">
            <div v-for="(change, field) in event.changes" :key="field">
              <dt>{{ fieldLabels[field] ?? field }}</dt>
              <dd>
                <span>{{ valueLabel(change.before) }}</span>
                <i>→</i>
                <strong>{{ valueLabel(change.after) }}</strong>
              </dd>
            </div>
          </dl>
        </article>
      </li>
    </ol>
  </div>
</template>

<style scoped>
.audit-timeline ol {
  display: grid;
  gap: 0;
  margin: 0;
  padding: 0;
  list-style: none;
}

.audit-timeline li {
  position: relative;
  display: grid;
  grid-template-columns: 18px minmax(0, 1fr);
  gap: 10px;
  padding-bottom: 14px;
}

.audit-timeline li::before {
  position: absolute;
  top: 12px;
  bottom: -2px;
  left: 5px;
  width: 1px;
  content: "";
  background: #dfe8e4;
}

.audit-timeline li:last-child::before {
  display: none;
}

.audit-timeline li > i {
  position: relative;
  z-index: 1;
  width: 11px;
  height: 11px;
  margin-top: 5px;
  border: 3px solid #d8eee4;
  border-radius: 50%;
  background: var(--green);
}

.audit-timeline article {
  min-width: 0;
  padding: 12px 13px;
  border: 1px solid #e2eae6;
  border-radius: 9px;
  background: #fbfdfc;
}

.audit-timeline header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.audit-timeline header b,
.audit-timeline header small {
  display: block;
}

.audit-timeline header b {
  color: #2e4038;
  font-size: 10px;
}

.audit-timeline header small,
.audit-timeline time {
  margin-top: 4px;
  color: var(--muted);
  font-size: 8px;
}

.audit-timeline time {
  white-space: nowrap;
}

.comment {
  margin: 10px 0 0;
  padding: 9px 10px;
  border-radius: 7px;
  color: #55665f;
  background: #f0f5f2;
  font-size: 9px;
  line-height: 1.6;
  white-space: pre-wrap;
}

.changes {
  display: grid;
  gap: 8px;
  margin: 10px 0 0;
}

.changes > div {
  display: grid;
  grid-template-columns: 78px minmax(0, 1fr);
  gap: 9px;
  padding-top: 8px;
  border-top: 1px solid #edf2ef;
}

.changes dt {
  color: #839089;
  font-size: 8px;
}

.changes dd {
  display: flex;
  min-width: 0;
  align-items: flex-start;
  gap: 7px;
  margin: 0;
  color: #79857f;
  font-size: 9px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.changes dd i {
  color: #a8b2ad;
  font-style: normal;
}

.changes dd strong {
  color: #33463d;
}

.audit-state {
  display: grid;
  min-height: 100px;
  place-content: center;
  justify-items: center;
  gap: 8px;
  margin: 0;
  color: var(--muted);
  font-size: 9px;
  text-align: center;
}

.audit-state--error {
  color: #a43f3f;
}

.spinner {
  width: 20px;
  height: 20px;
  border: 2px solid #deebe5;
  border-top-color: var(--green);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

@media (max-width: 700px) {
  .audit-timeline header,
  .changes dd {
    flex-direction: column;
  }

  .changes > div {
    grid-template-columns: 1fr;
  }
}
</style>
