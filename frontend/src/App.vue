<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import AppLayout from "@/layouts/AppLayout.vue";

const route = useRoute();
const standalone = computed(() => route.meta.standalone === true);
</script>

<template>
  <RouterView v-if="standalone" />
  <AppLayout v-else />
</template>
