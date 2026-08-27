<script setup lang="ts">
import { computed } from "vue";
const props = defineProps<{ status: string; label?: string }>();
const tone = computed(() => {
  if (["active", "ready", "passed", "succeeded", "completed", "ok", "verified", "closed"].includes(props.status)) return "success";
  if (["running", "in_progress"].includes(props.status)) return "running";
  if (["failed", "blocked", "reopened", "blocker", "critical"].includes(props.status)) return "danger";
  if (["queued", "created", "draft", "not_run", "open", "resolved"].includes(props.status)) return "warning";
  return "neutral";
});
</script>

<template><span class="badge" :class="`badge--${tone}`"><i></i>{{ label ?? status }}</span></template>
