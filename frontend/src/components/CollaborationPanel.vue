<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { ApiError, qaApi } from "@/api";
import type { AttachmentRecord, CollaborationEntityType, CommentRecord } from "@/api";
import { useAuthSession } from "@/auth/session";

const props = withDefaults(
  defineProps<{
    projectId: string;
    entityId: string;
    entityType?: CollaborationEntityType;
  }>(),
  { entityType: "defect" },
);
const auth = useAuthSession();
const comments = ref<CommentRecord[]>([]);
const attachments = ref<AttachmentRecord[]>([]);
const body = ref("");
const loading = ref(false);
const writing = ref(false);
const errorMessage = ref("");
const editingId = ref<string | null>(null);
const editingBody = ref("");
const previews = new Map<string, string>();
const previewUrls = ref<Record<string, string>>({});
const canWrite = computed(() => auth.can("collaboration.write"));
const canModerateComments = computed(() => auth.can("comment.moderate"));
const canModerateAttachments = computed(() => auth.can("attachment.moderate"));
let loadVersion = 0;

onMounted(load);
watch(() => [props.entityType, props.entityId] as const, load);
onBeforeUnmount(() => {
  loadVersion += 1;
  clearPreviews();
});

async function load() {
  if (!props.entityId) return;
  const requestVersion = ++loadVersion;
  const entityType = props.entityType;
  const entityId = props.entityId;
  loading.value = true;
  errorMessage.value = "";
  clearPreviews();
  try {
    const [nextComments, nextAttachments] = await Promise.all([
      qaApi.listComments(entityType, entityId),
      qaApi.listAttachments(entityType, entityId),
    ]);
    if (
      requestVersion === loadVersion &&
      entityType === props.entityType &&
      entityId === props.entityId
    ) {
      comments.value = nextComments;
      attachments.value = nextAttachments;
    }
  } catch (error) {
    if (requestVersion === loadVersion) {
      errorMessage.value = error instanceof ApiError ? error.message : "协作记录加载失败";
    }
  } finally {
    if (requestVersion === loadVersion) loading.value = false;
  }
}

function canManageComment(comment: CommentRecord): boolean {
  return Boolean(
    canWrite.value &&
      !comment.deleted_at &&
      (comment.author_id === auth.state.user?.id || canModerateComments.value),
  );
}

function canManageAttachment(attachment: AttachmentRecord): boolean {
  return Boolean(
    canWrite.value &&
      (attachment.uploader_id === auth.state.user?.id || canModerateAttachments.value),
  );
}

async function addComment() {
  if (!body.value.trim()) return;
  writing.value = true;
  errorMessage.value = "";
  try {
    await qaApi.createComment({
      project_id: props.projectId,
      entity_type: props.entityType,
      entity_id: props.entityId,
      body: body.value.trim(),
    });
    body.value = "";
    await load();
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : "评论提交失败";
  } finally {
    writing.value = false;
  }
}

function beginEdit(comment: CommentRecord) {
  editingId.value = comment.id;
  editingBody.value = comment.body;
}

async function saveEdit() {
  if (!editingId.value || !editingBody.value.trim()) return;
  writing.value = true;
  try {
    await qaApi.updateComment(editingId.value, editingBody.value.trim());
    editingId.value = null;
    await load();
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : "评论修改失败";
  } finally {
    writing.value = false;
  }
}

async function removeComment(comment: CommentRecord) {
  if (!window.confirm("确认删除这条评论？审计记录仍会保留。")) return;
  writing.value = true;
  try {
    await qaApi.deleteComment(comment.id);
    await load();
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : "评论删除失败";
  } finally {
    writing.value = false;
  }
}

async function upload(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = "";
  if (!file) return;
  writing.value = true;
  errorMessage.value = "";
  try {
    await qaApi.uploadAttachment(
      props.projectId,
      props.entityType,
      props.entityId,
      file,
    );
    await load();
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : "附件上传失败";
  } finally {
    writing.value = false;
  }
}

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

async function downloadFile(attachment: AttachmentRecord) {
  try {
    const file = await qaApi.downloadAttachment(attachment.id);
    saveBlob(file.blob, file.filename ?? attachment.original_filename);
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : "附件下载失败";
  }
}

async function togglePreview(attachment: AttachmentRecord) {
  const existing = previews.get(attachment.id);
  if (existing) {
    URL.revokeObjectURL(existing);
    previews.delete(attachment.id);
    previewUrls.value = Object.fromEntries(previews);
    return;
  }
  try {
    const file = await qaApi.downloadAttachment(attachment.id, true);
    const url = URL.createObjectURL(file.blob);
    previews.set(attachment.id, url);
    previewUrls.value = Object.fromEntries(previews);
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : "图片预览失败";
  }
}

async function removeAttachment(attachment: AttachmentRecord) {
  if (!window.confirm(`确认移除附件“${attachment.original_filename}”？`)) return;
  writing.value = true;
  try {
    await qaApi.deleteAttachment(attachment.id);
    await load();
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : "附件移除失败";
  } finally {
    writing.value = false;
  }
}

function clearPreviews() {
  previews.forEach((url) => URL.revokeObjectURL(url));
  previews.clear();
  previewUrls.value = {};
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / 1024 / 1024).toFixed(1)} MiB`;
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "short" }).format(new Date(value));
}
</script>

<template>
  <section class="collaboration">
    <div class="collab-head"><div><small>COLLABORATION</small><h3>评论、图片与附件</h3></div><button class="link-button" :disabled="loading" @click="load">刷新</button></div>
    <p v-if="errorMessage" class="collab-error">{{ errorMessage }}</p>

    <form v-if="canWrite" class="comment-form" @submit.prevent="addComment">
      <textarea v-model="body" maxlength="4000" placeholder="补充复现信息、验证结论或协作说明…"></textarea>
      <div><small>{{ body.length }}/4000</small><button class="button primary" :disabled="writing || !body.trim()">发表评论</button></div>
    </form>

    <div class="comment-list">
      <article v-for="comment in comments" :key="comment.id">
        <header><div><b>{{ comment.deleted_at ? "已删除评论" : comment.author_name }}</b><small>{{ formatTime(comment.created_at) }}<em v-if="comment.edited_at && !comment.deleted_at"> · 已编辑</em></small></div><div v-if="canManageComment(comment)"><button @click="beginEdit(comment)">编辑</button><button @click="removeComment(comment)">删除</button></div></header>
        <template v-if="!comment.deleted_at && editingId === comment.id"><textarea v-model="editingBody" maxlength="4000"></textarea><div class="edit-actions"><button class="button" @click="editingId=null">取消</button><button class="button primary" :disabled="writing || !editingBody.trim()" @click="saveEdit">保存</button></div></template>
        <p v-else :class="{ tombstone: comment.deleted_at }">{{ comment.deleted_at ? "这条评论已删除；保留占位以维持审计和回复关系。" : comment.body }}</p>
      </article>
      <p v-if="!comments.length && !loading" class="empty-mini">还没有评论。</p>
    </div>

    <div class="attachment-head"><b>附件</b><label v-if="canWrite" :class="{ disabled: writing }">上传文件<input type="file" accept="image/png,image/jpeg,image/webp,application/pdf,text/plain,text/csv,application/json,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" :disabled="writing" @change="upload" /></label></div>
    <div class="attachment-list">
      <article v-for="attachment in attachments" :key="attachment.id">
        <div><b>{{ attachment.original_filename }}</b><small>{{ attachment.media_type }} · {{ formatBytes(attachment.size_bytes) }} · {{ attachment.uploader_name }}</small><code>SHA-256 {{ attachment.sha256.slice(0,16) }}…</code></div>
        <div><button v-if="attachment.is_image" @click="togglePreview(attachment)">{{ previewUrls[attachment.id] ? "收起" : "预览" }}</button><button @click="downloadFile(attachment)">下载</button><button v-if="canManageAttachment(attachment)" @click="removeAttachment(attachment)">移除</button></div>
        <img v-if="previewUrls[attachment.id]" :src="previewUrls[attachment.id]" :alt="attachment.original_filename" />
      </article>
      <p v-if="!attachments.length && !loading" class="empty-mini">还没有附件；文件只通过鉴权下载接口访问。</p>
    </div>
  </section>
</template>

<style scoped>
.collaboration{margin-top:18px;padding:18px;border:1px solid #dfe7e3;border-radius:12px;background:#fbfdfc}.collab-head,.attachment-head{display:flex;align-items:center;justify-content:space-between}.collab-head small{color:var(--green);font-size:8px;letter-spacing:.14em}.collab-head h3{margin:5px 0 0;font-size:14px}.link-button,.comment-list header button,.attachment-list button{padding:4px 6px;border:0;color:var(--green);background:transparent;font-size:8px}.collab-error{padding:9px;border-radius:7px;color:#a84040;background:#fff0f0;font-size:9px}.comment-form{display:grid;gap:8px;margin-top:14px}.comment-form textarea,.comment-list textarea{width:100%;min-height:82px;padding:10px;border:1px solid #d8e3dd;border-radius:8px;resize:vertical;font:inherit;font-size:10px}.comment-form>div{display:flex;align-items:center;justify-content:space-between}.comment-form small{color:var(--muted);font-size:8px}.comment-list{display:grid;gap:8px;margin-top:14px}.comment-list article{padding:12px;border:1px solid #e4ebe7;border-radius:9px;background:#fff}.comment-list header{display:flex;justify-content:space-between}.comment-list header b,.comment-list header small{display:block}.comment-list header b{font-size:10px}.comment-list header small{margin-top:3px;color:var(--muted);font-size:8px}.comment-list header em{font-style:normal}.comment-list p{margin:10px 0 0;color:#4e5b55;font-size:10px;line-height:1.7;white-space:pre-wrap}.comment-list .tombstone{color:var(--muted);font-style:italic}.edit-actions{display:flex;justify-content:flex-end;gap:6px;margin-top:6px}.attachment-head{margin-top:17px;padding-top:14px;border-top:1px solid #e6ece9}.attachment-head>b{font-size:10px}.attachment-head label{padding:6px 9px;border-radius:7px;color:#fff;background:var(--green);font-size:8px;cursor:pointer}.attachment-head label.disabled{opacity:.6}.attachment-head input{position:absolute;opacity:0;pointer-events:none}.attachment-list{display:grid;gap:7px;margin-top:9px}.attachment-list article{display:grid;grid-template-columns:1fr auto;gap:9px;padding:10px;border:1px solid #e4ebe7;border-radius:9px;background:#fff}.attachment-list b,.attachment-list small,.attachment-list code{display:block}.attachment-list b{font-size:9px}.attachment-list small{margin-top:3px;color:var(--muted);font-size:7px}.attachment-list code{margin-top:4px;color:#819089;font-size:6px}.attachment-list img{grid-column:1/-1;max-width:min(100%,720px);max-height:420px;border-radius:8px;object-fit:contain;background:#eef3f0}.empty-mini{margin:0;padding:12px;color:var(--muted);text-align:center;font-size:9px}
</style>
