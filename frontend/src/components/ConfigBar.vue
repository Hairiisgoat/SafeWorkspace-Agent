<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import {
  Check,
  ChevronDown,
  Clock,
  FolderLock,
  FolderOpen,
  HardDrive,
  History,
  KeyRound,
  Loader2,
  Moon,
  RefreshCw,
  Settings2,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Sun,
} from 'lucide-vue-next'
import { useWorkspaceStore } from '@/stores/workspace'

const store = useWorkspaceStore()

const saving = ref(false)
const refreshing = ref(false)
const switchingWorkspace = ref(false)
const manualPathInput = ref('')
const workspaceError = ref('')

const form = reactive({
  sandbox_root: '',
  api_key: '',
  base_url: 'https://api.openai.com/v1',
  model_name: 'gpt-4o-mini',
})

const providerPresets = [
  {
    label: 'OpenAI',
    base_url: 'https://api.openai.com/v1',
    model_name: 'gpt-4o-mini',
  },
  {
    label: 'DeepSeek',
    base_url: 'https://api.deepseek.com/v1',
    model_name: 'deepseek-chat',
  },
  {
    label: 'OpenRouter',
    base_url: 'https://openrouter.ai/api/v1',
    model_name: 'anthropic/claude-3.5-sonnet',
  },
  {
    label: 'Ollama (本地)',
    base_url: 'http://localhost:11434/v1',
    model_name: 'qwen2.5:14b',
  },
]

const recentWorkspaces = computed(() => store.config?.recent_workspaces || [])

watch(
  () => store.workspacePanelOpen,
  (open) => {
    if (open) {
      manualPathInput.value = store.config?.sandbox_root || ''
      workspaceError.value = ''
    }
  },
)

watch(
  () => store.configDialogOpen,
  (open) => {
    if (open && store.config) {
      form.sandbox_root = store.config.sandbox_root
      form.base_url = store.config.base_url
      form.model_name = store.config.model_name
      form.api_key = ''
    }
  },
)

function applyPreset(preset: { base_url: string; model_name: string }) {
  form.base_url = preset.base_url
  form.model_name = preset.model_name
}

function formatBytes(bytes: number): string {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  return `${(bytes / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

function extractFolderName(fullPath: string): string {
  const cleaned = fullPath.replace(/[\\/]+$/, '')
  const parts = cleaned.split(/[\\/]/)
  return parts[parts.length - 1] || fullPath
}

function isCurrentWorkspace(path: string): boolean {
  const active = (store.config?.sandbox_root || '').replace(/[\\/]+$/, '').toLowerCase()
  return path.replace(/[\\/]+$/, '').toLowerCase() === active
}

async function handleRefresh() {
  refreshing.value = true
  try {
    await store.refreshWorkspace()
    ElMessage.success('沙盒工作区状态已刷新')
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '刷新失败')
  } finally {
    refreshing.value = false
  }
}

async function handleSeedDemo() {
  try {
    const res = await store.seedDemoWorkspace()
    if (res.created_files.length > 0) {
      ElMessage.success(`已在沙盒内生成 ${res.created_files.length} 个示例杂乱文件`)
    } else {
      ElMessage.info('示例文件均已存在，未覆盖任何本地文件')
    }
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '生成示例文件失败')
  }
}

async function applyWorkspacePath(targetPath: string) {
  const trimmed = targetPath.trim()
  if (!trimmed) {
    workspaceError.value = '请输入有效的本地目录路径'
    return
  }
  workspaceError.value = ''
  switchingWorkspace.value = true
  try {
    const updated = await store.switchWorkspace(trimmed)
    manualPathInput.value = updated.sandbox_root
    store.workspacePanelOpen = false
    ElMessage.success(`已切换沙盒工作区：${extractFolderName(updated.sandbox_root)}`)
  } catch (err) {
    const msg = err instanceof Error ? err.message : '切换工作区失败'
    workspaceError.value = msg
  } finally {
    switchingWorkspace.value = false
  }
}

async function handleBrowseLocalFolder(target: 'panel' | 'modal') {
  workspaceError.value = ''
  try {
    const res = await store.selectWorkspaceViaDialog()
    if (res.cancelled) {
      if (res.error) {
        ElMessage.warning(res.error)
      }
      return
    }
    if (!res.valid || !res.selected_path) {
      const msg = res.error || '所选目录未通过 SandboxGuard 安全校验'
      if (target === 'panel') {
        workspaceError.value = msg
      } else {
        ElMessage.error(msg)
      }
      return
    }

    if (target === 'modal') {
      form.sandbox_root = res.selected_path
      ElMessage.success(`已选中目录：${extractFolderName(res.selected_path)}`)
    } else {
      manualPathInput.value = res.selected_path
      await applyWorkspacePath(res.selected_path)
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : '打开系统文件夹选择对话框失败'
    if (target === 'panel') {
      workspaceError.value = msg
    } else {
      ElMessage.error(msg)
    }
  }
}

async function handleSave() {
  saving.value = true
  try {
    const payload: Record<string, string> = {}
    if (form.sandbox_root.trim()) payload.sandbox_root = form.sandbox_root.trim()
    if (form.base_url.trim()) payload.base_url = form.base_url.trim()
    if (form.model_name.trim()) payload.model_name = form.model_name.trim()
    if (form.api_key.trim()) payload.api_key = form.api_key.trim()

    await store.saveConfig(payload)
    store.configDialogOpen = false
    ElMessage.success('配置已生效并永久保存至本地 .local_config.json')
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '保存配置失败')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <header class="config-bar">
    <!-- Brand -->
    <div class="brand-group">
      <div class="brand-logo">
        <ShieldCheck :size="17" :stroke-width="2.2" />
      </div>
      <div class="brand-meta">
        <span class="brand-title">SafeWorkspace</span>
        <span class="brand-tag">Agent</span>
      </div>
      <span class="security-pill" title="LLM 仅拥有 L1-L3 只读探针与 Dry-Run 提案权限，零直接写权限">
        <span class="dot-emerald" />
        Strict Sandbox
      </span>
    </div>

    <!-- Center Dynamic Workspace Switcher Pill -->
    <div class="sandbox-center">
      <el-popover
        v-model:visible="store.workspacePanelOpen"
        placement="bottom"
        :width="500"
        trigger="click"
        popper-class="workspace-switcher-popper"
      >
        <template #reference>
          <button
            type="button"
            class="sandbox-pill"
            :class="{ open: store.workspacePanelOpen }"
            title="点击切换本地沙盒工作区 (浏览文件夹 / 最近使用目录)"
          >
            <FolderLock :size="14" class="icon-muted" />
            <span class="sandbox-label">SANDBOX_ROOT</span>
            <span class="sandbox-path mono">
              {{ store.config?.sandbox_root || '加载中...' }}
            </span>
            <span v-if="store.config" class="sandbox-metrics mono">
              {{ store.config.file_count }} files · {{ store.config.dir_count }} dirs ·
              {{ formatBytes(store.config.total_size_bytes) }}
            </span>
            <ChevronDown
              :size="13"
              class="icon-muted chevron-icon"
              :class="{ rotated: store.workspacePanelOpen }"
            />
          </button>
        </template>

        <!-- Popover Content: Workspace Switcher Panel -->
        <div class="ws-panel">
          <div class="ws-panel-head">
            <div class="ws-head-title">
              <HardDrive :size="14" />
              <span>切换安全沙盒工作区</span>
            </div>
            <span class="ws-guard-tag">
              <ShieldCheck :size="11" />
              <span>SandboxGuard 保护</span>
            </span>
          </div>

          <!-- Native OS Folder Dialog CTA -->
          <div class="ws-browse-row">
            <button
              type="button"
              class="btn-browse-primary"
              :disabled="store.isPickingDirectory || switchingWorkspace"
              @click="handleBrowseLocalFolder('panel')"
            >
              <Loader2 v-if="store.isPickingDirectory" :size="14" class="spinning" />
              <FolderOpen v-else :size="14" />
              <span>
                {{
                  store.isPickingDirectory
                    ? '等待系统文件夹弹窗选择...'
                    : '浏览本地文件夹...'
                }}
              </span>
            </button>
          </div>

          <!-- Manual Input Row -->
          <div class="ws-manual-row">
            <el-input
              v-model="manualPathInput"
              size="default"
              placeholder="或粘贴/输入本地目录绝对路径，按 Enter 切换"
              class="mono-input"
              :disabled="switchingWorkspace"
              @keyup.enter="applyWorkspacePath(manualPathInput)"
            />
            <button
              type="button"
              class="btn-switch"
              :disabled="switchingWorkspace || !manualPathInput.trim()"
              @click="applyWorkspacePath(manualPathInput)"
            >
              {{ switchingWorkspace ? '校验中...' : '切换' }}
            </button>
          </div>

          <!-- SandboxGuard Security Error Banner -->
          <div v-if="workspaceError" class="ws-error-box">
            <ShieldAlert :size="14" class="ws-error-icon" />
            <span>{{ workspaceError }}</span>
          </div>

          <!-- Recent Workspaces List -->
          <div v-if="recentWorkspaces.length > 0" class="ws-recent-section">
            <div class="ws-recent-head">
              <Clock :size="12" />
              <span>最近使用目录 ({{ recentWorkspaces.length }}/5)</span>
            </div>
            <div class="ws-recent-list">
              <button
                v-for="wsPath in recentWorkspaces"
                :key="wsPath"
                type="button"
                class="ws-recent-item"
                :class="{ current: isCurrentWorkspace(wsPath) }"
                :disabled="switchingWorkspace"
                @click="
                  isCurrentWorkspace(wsPath)
                    ? (store.workspacePanelOpen = false)
                    : applyWorkspacePath(wsPath)
                "
              >
                <div class="ws-recent-main">
                  <span class="ws-recent-name">{{ extractFolderName(wsPath) }}</span>
                  <span class="ws-recent-path mono" :title="wsPath">{{ wsPath }}</span>
                </div>
                <span v-if="isCurrentWorkspace(wsPath)" class="ws-current-pill">
                  <Check :size="11" />
                  <span>当前</span>
                </span>
              </button>
            </div>
          </div>

          <!-- Footer Guardrail Notice -->
          <div class="ws-panel-footer">
            <span class="ws-footnote">
              已拦截盘符根目录 (C:\)、系统目录及用户主目录根层级
            </span>
            <button
              type="button"
              class="text-action-btn"
              :disabled="store.isSeeding"
              @click="handleSeedDemo"
            >
              <Sparkles :size="12" />
              <span>生成示例文件</span>
            </button>
          </div>
        </div>
      </el-popover>

      <button
        type="button"
        class="icon-btn"
        :disabled="refreshing"
        title="重新扫描当前沙盒目录"
        @click="handleRefresh"
      >
        <RefreshCw :size="14" :class="{ spinning: refreshing }" />
      </button>

      <button
        v-if="store.config && store.config.file_count === 0"
        type="button"
        class="subtle-btn"
        :disabled="store.isSeeding"
        @click="handleSeedDemo"
      >
        <Sparkles :size="13" />
        <span>生成示例文件</span>
      </button>
    </div>

    <!-- Right Controls -->
    <div class="right-controls">
      <button
        type="button"
        class="history-pill"
        :class="{ active: store.activeRightTab === 'history' }"
        title="查看 .agent_history 事务回滚日志"
        @click="store.activeRightTab = store.activeRightTab === 'history' ? 'diff' : 'history'"
      >
        <History :size="14" />
        <span>事务历史</span>
        <span v-if="store.undoableBatchCount > 0" class="count-badge mono">
          {{ store.undoableBatchCount }}
        </span>
      </button>

      <button
        type="button"
        class="model-pill"
        :class="{ warning: !store.isModelReady }"
        title="配置 OpenAI 兼容 BYOK 模型与 API Key（本地持久化，重启免重填）"
        @click="store.configDialogOpen = true"
      >
        <span :class="store.isModelReady ? 'dot-emerald' : 'dot-amber'" />
        <span class="mono model-name">{{ store.config?.model_name || 'gpt-4o-mini' }}</span>
        <Settings2 :size="13" class="icon-muted" />
      </button>

      <button
        type="button"
        class="icon-btn"
        :title="store.darkMode ? '切换浅色极简模式' : '切换深色极简模式'"
        @click="store.toggleDarkMode()"
      >
        <Sun v-if="store.darkMode" :size="15" />
        <Moon v-else :size="15" />
      </button>
    </div>

    <!-- BYOK & Sandbox Configuration Modal -->
    <el-dialog
      v-model="store.configDialogOpen"
      width="580px"
      align-center
      :close-on-click-modal="false"
    >
      <template #header>
        <div class="dialog-title-row">
          <KeyRound :size="16" />
          <span>沙盒工作区与 BYOK 模型配置</span>
          <span
            v-if="store.config?.persisted"
            class="persisted-pill"
            title="配置已持久化在 backend/.local_config.json（已加入 .gitignore）"
          >
            本地已持久化 · 重启免重填
          </span>
        </div>
      </template>

      <div class="dialog-body">
        <!-- Sandbox Section -->
        <section class="config-section">
          <div class="section-head">
            <span class="section-title">1. 严格沙盒根目录 (SANDBOX_ROOT)</span>
            <button
              type="button"
              class="text-action-btn"
              :disabled="store.isSeeding"
              @click="handleSeedDemo"
            >
              <Sparkles :size="12" />
              <span>向此目录写入示例测试文件</span>
            </button>
          </div>
          <div class="modal-path-row">
            <el-input
              v-model="form.sandbox_root"
              placeholder="例如：./demo_sandbox 或 D:\Workspaces\my_docs"
              class="mono-input"
            />
            <button
              type="button"
              class="btn-secondary modal-browse-btn"
              :disabled="store.isPickingDirectory"
              @click="handleBrowseLocalFolder('modal')"
            >
              <Loader2 v-if="store.isPickingDirectory" :size="13" class="spinning" />
              <FolderOpen v-else :size="13" />
              <span>浏览...</span>
            </button>
          </div>
          <p class="field-hint">
            路径守卫 (<code>SandboxGuard</code>) 强制拦截盘符根目录、系统目录、用户主目录根层级、<code>../</code> 路径穿越与
            <code>.agent_history</code> 内部回滚日志。
          </p>
        </section>

        <!-- BYOK LLM Section -->
        <section class="config-section">
          <div class="section-head">
            <span class="section-title">2. OpenAI 兼容模型 (BYOK)</span>
            <div class="preset-row">
              <button
                v-for="p in providerPresets"
                :key="p.label"
                type="button"
                class="preset-chip"
                @click="applyPreset(p)"
              >
                {{ p.label }}
              </button>
            </div>
          </div>

          <div class="form-grid">
            <div class="form-field">
              <label class="field-label">API Base URL</label>
              <el-input
                v-model="form.base_url"
                placeholder="https://api.openai.com/v1"
                class="mono-input"
              />
            </div>

            <div class="form-field">
              <label class="field-label">Model Name</label>
              <el-input
                v-model="form.model_name"
                placeholder="gpt-4o-mini / deepseek-chat / qwen2.5"
                class="mono-input"
              />
            </div>

            <div class="form-field full-span">
              <div class="label-row">
                <label class="field-label">API Key</label>
                <span v-if="store.config?.has_api_key" class="key-status mono">
                  已配置：{{ store.config.api_key_masked }}
                </span>
                <span v-else class="key-status missing">尚未配置 API Key</span>
              </div>
              <el-input
                v-model="form.api_key"
                type="password"
                show-password
                autocomplete="off"
                :placeholder="
                  store.config?.has_api_key
                    ? '留空则沿用已持久化的本地密钥，或输入新密钥覆盖'
                    : '输入您的 sk-... 密钥（Ollama 本地模型可留空）'
                "
                class="mono-input"
              />
              <p class="field-hint">
                保存后自动写入 <code>backend/.local_config.json</code>（已加入 <code>.gitignore</code> 防止泄露），下次启动后端自动加载。
              </p>
            </div>
          </div>
        </section>
      </div>

      <template #footer>
        <div class="dialog-footer">
          <button
            type="button"
            class="btn-secondary"
            @click="store.configDialogOpen = false"
          >
            取消
          </button>
          <button
            type="button"
            class="btn-primary"
            :disabled="saving"
            @click="handleSave"
          >
            {{ saving ? '验证并持久化中...' : '保存并持久化' }}
          </button>
        </div>
      </template>
    </el-dialog>
  </header>
</template>

<style scoped>
.config-bar {
  height: 54px;
  padding: 0 18px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  background-color: var(--bg-surface);
  border-bottom: 1px solid var(--border-default);
  flex-shrink: 0;
  z-index: 20;
}

.brand-group {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}

.brand-logo {
  width: 28px;
  height: 28px;
  border-radius: 7px;
  background: var(--cta-bg);
  color: var(--cta-text);
  display: flex;
  align-items: center;
  justify-content: center;
}

.brand-meta {
  display: flex;
  align-items: baseline;
  gap: 5px;
}

.brand-title {
  font-weight: 650;
  font-size: 14.5px;
  letter-spacing: -0.02em;
  color: var(--text-primary);
}

.brand-tag {
  font-size: 11px;
  font-weight: 500;
  color: var(--text-muted);
}

.security-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 500;
  color: var(--plan-text);
  background: var(--plan-bg);
  border: 1px solid var(--plan-border);
}

.sandbox-center {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex: 1;
  justify-content: center;
}

.sandbox-pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  max-width: 640px;
  min-width: 0;
  padding: 5px 11px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-default);
  background: var(--bg-canvas);
  color: var(--text-primary);
  cursor: pointer;
  transition: border-color 0.15s ease, background-color 0.15s ease, box-shadow 0.15s ease;
}

.sandbox-pill:hover,
.sandbox-pill.open {
  border-color: var(--border-strong);
  background: var(--bg-hover);
}

.sandbox-pill.open {
  border-color: var(--text-primary);
}

.sandbox-label {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--text-muted);
  flex-shrink: 0;
}

.sandbox-path {
  font-size: 12px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sandbox-metrics {
  font-size: 11px;
  color: var(--text-muted);
  padding-left: 8px;
  border-left: 1px solid var(--border-default);
  flex-shrink: 0;
}

.chevron-icon {
  transition: transform 0.16s ease;
}

.chevron-icon.rotated {
  transform: rotate(180deg);
}

/* Workspace Switcher Popover Panel */
.ws-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 2px;
}

.ws-panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.ws-head-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.ws-guard-tag {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 10.5px;
  font-weight: 500;
  padding: 2px 7px;
  border-radius: 999px;
  background: var(--plan-bg);
  color: var(--plan-text);
  border: 1px solid var(--plan-border);
}

.ws-browse-row {
  display: flex;
}

.btn-browse-primary {
  width: 100%;
  height: 36px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  border-radius: var(--radius-md);
  border: 1px solid transparent;
  background: var(--cta-bg);
  color: var(--cta-text);
  font-size: 12.5px;
  font-weight: 550;
  cursor: pointer;
  transition: background-color 0.15s ease;
}

.btn-browse-primary:hover:not(:disabled) {
  background: var(--cta-bg-hover);
}

.btn-browse-primary:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.ws-manual-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.btn-switch {
  height: 32px;
  padding: 0 13px;
  flex-shrink: 0;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-strong);
  background: var(--bg-surface);
  color: var(--text-primary);
  font-size: 12px;
  font-weight: 550;
  cursor: pointer;
  transition: all 0.14s ease;
}

.btn-switch:hover:not(:disabled) {
  background: var(--bg-hover);
  border-color: var(--text-primary);
}

.btn-switch:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.ws-error-box {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  padding: 8px 10px;
  border-radius: var(--radius-md);
  background: var(--danger-bg);
  border: 1px solid var(--danger-border);
  color: var(--danger-text);
  font-size: 11.5px;
  line-height: 1.5;
}

.ws-error-icon {
  flex-shrink: 0;
  margin-top: 2px;
}

.ws-recent-section {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-top: 4px;
  border-top: 1px solid var(--border-subtle);
}

.ws-recent-head {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-muted);
}

.ws-recent-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.ws-recent-item {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 7px 9px;
  border-radius: var(--radius-md);
  border: 1px solid transparent;
  background: var(--bg-canvas);
  text-align: left;
  cursor: pointer;
  transition: all 0.12s ease;
}

.ws-recent-item:hover:not(:disabled) {
  background: var(--bg-hover);
  border-color: var(--border-default);
}

.ws-recent-item.current {
  border-color: var(--plan-border);
  background: var(--plan-bg);
}

.ws-recent-main {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
}

.ws-recent-name {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ws-recent-path {
  font-size: 11px;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ws-current-pill {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 10.5px;
  font-weight: 600;
  color: var(--plan-text);
  flex-shrink: 0;
}

.ws-panel-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--border-subtle);
}

.ws-footnote {
  font-size: 10.5px;
  color: var(--text-faint);
}

.right-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.history-pill,
.model-pill,
.subtle-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 30px;
  padding: 0 10px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  color: var(--text-primary);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s ease;
}

.history-pill:hover,
.model-pill:hover,
.subtle-btn:hover {
  background: var(--bg-hover);
  border-color: var(--border-strong);
}

.history-pill.active {
  background: var(--bg-subtle);
  border-color: var(--text-primary);
}

.model-pill.warning {
  border-color: var(--l3-border);
  background: var(--l3-bg);
}

.model-name {
  font-size: 11.5px;
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.count-badge {
  font-size: 10.5px;
  padding: 0 5px;
  height: 17px;
  line-height: 17px;
  border-radius: 999px;
  background: var(--cta-bg);
  color: var(--cta-text);
}

.icon-btn {
  width: 30px;
  height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  color: var(--text-secondary);
  cursor: pointer;
  transition: all 0.15s ease;
}

.icon-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.icon-muted {
  color: var(--text-muted);
  flex-shrink: 0;
}

.dot-emerald,
.dot-amber {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}

.dot-emerald {
  background-color: #10b981;
}

.dot-amber {
  background-color: #f59e0b;
}

.spinning {
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

/* Modal Styles */
.dialog-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  font-size: 14.5px;
  color: var(--text-primary);
}

.persisted-pill {
  margin-left: auto;
  font-size: 10.5px;
  font-weight: 550;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--plan-bg);
  color: var(--plan-text);
  border: 1px solid var(--plan-border);
}

.dialog-body {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.config-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.modal-path-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.modal-browse-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex-shrink: 0;
}

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}

.section-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text-primary);
}

.text-action-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: none;
  background: transparent;
  color: var(--l1-text);
  font-size: 11.5px;
  font-weight: 500;
  cursor: pointer;
  padding: 2px 5px;
  border-radius: 4px;
  flex-shrink: 0;
}

.text-action-btn:hover {
  background: var(--l1-bg);
}

.preset-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.preset-chip {
  padding: 2px 8px;
  font-size: 11px;
  font-weight: 500;
  border-radius: 999px;
  border: 1px solid var(--border-default);
  background: var(--bg-canvas);
  color: var(--text-secondary);
  cursor: pointer;
  transition: all 0.12s ease;
}

.preset-chip:hover {
  border-color: var(--text-primary);
  color: var(--text-primary);
}

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.form-field {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.form-field.full-span {
  grid-column: 1 / -1;
}

.label-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.field-label {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
}

.key-status {
  font-size: 11px;
  color: var(--plan-text);
}

.key-status.missing {
  color: var(--l3-text);
}

.field-hint {
  margin: 0;
  font-size: 11.5px;
  color: var(--text-muted);
  line-height: 1.5;
}

.field-hint code {
  font-family: var(--font-mono);
  font-size: 11px;
  background: var(--bg-subtle);
  padding: 1px 4px;
  border-radius: 4px;
}

.mono-input :deep(input) {
  font-family: var(--font-mono);
  font-size: 12px;
}

.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.btn-secondary,
.btn-primary {
  height: 32px;
  padding: 0 14px;
  border-radius: var(--radius-md);
  font-size: 12.5px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s ease;
}

.btn-secondary {
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  color: var(--text-secondary);
}

.btn-secondary:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.btn-primary {
  border: 1px solid transparent;
  background: var(--cta-bg);
  color: var(--cta-text);
}

.btn-primary:hover:not(:disabled) {
  background: var(--cta-bg-hover);
}

.btn-primary:disabled,
.btn-secondary:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

@media (max-width: 1024px) {
  .sandbox-metrics,
  .security-pill {
    display: none;
  }
}
</style>
