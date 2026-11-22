<script setup lang="ts">
import { onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import ConfigBar from '@/components/ConfigBar.vue'
import AgentTraceLog from '@/components/AgentTraceLog.vue'
import DiffTreePreview from '@/components/DiffTreePreview.vue'
import { useWorkspaceStore } from '@/stores/workspace'

const store = useWorkspaceStore()

onMounted(async () => {
  try {
    await store.initialize()
  } catch (err) {
    ElMessage.error(
      err instanceof Error
        ? `无法连接后端服务：${err.message}`
        : '无法连接 SafeWorkspace Agent 后端服务 (请检查 8000 端口)',
    )
  }
})
</script>

<template>
  <div class="workbench-shell">
    <ConfigBar />

    <div class="workbench-body">
      <div class="left-pane">
        <AgentTraceLog />
      </div>
      <div class="right-pane">
        <DiffTreePreview />
      </div>
    </div>
  </div>
</template>

<style scoped>
.workbench-shell {
  display: flex;
  flex-direction: column;
  height: 100vh;
  width: 100vw;
  overflow: hidden;
  background: var(--bg-canvas);
}

.workbench-body {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(360px, 40%) minmax(0, 60%);
}

.left-pane,
.right-pane {
  min-height: 0;
  min-width: 0;
  height: 100%;
}

@media (max-width: 960px) {
  .workbench-shell {
    height: auto;
    min-height: 100vh;
    overflow: auto;
  }

  .workbench-body {
    grid-template-columns: 1fr;
    grid-template-rows: auto auto;
  }

  .left-pane {
    height: 520px;
  }

  .right-pane {
    min-height: 600px;
  }
}
</style>
