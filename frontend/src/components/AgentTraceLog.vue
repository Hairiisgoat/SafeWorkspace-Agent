<script setup lang="ts">
import { computed, nextTick, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import {
  AlertTriangle,
  Bot,
  Brain,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CornerDownLeft,
  Eye,
  FileSearch,
  FolderSearch,
  Layers,
  Loader2,
  MessageSquarePlus,
  PauseCircle,
  Settings,
  ShieldAlert,
  Square,
  Trash2,
  User,
} from 'lucide-vue-next'
import { useWorkspaceStore, type DisplayTraceItem } from '@/stores/workspace'

const store = useWorkspaceStore()

const promptInput = ref('')
const textareaRef = ref<HTMLTextAreaElement | null>(null)
const scrollContainer = ref<HTMLElement | null>(null)
const expandedEventIds = reactive<Record<string, boolean>>({})

const quickPresets = [
  '按文档、财务发票、工程代码与归档目录自动整理，并规范重命名模糊文件',
  '优先识别无意义文件名（如 scan/IMG/untitled），归类整理并保留原扩展名',
  '把过时或旧版文件归档到 Archive/，其余按主题建立清晰目录结构',
]

const phaseBadge = computed(() => {
  switch (store.agentPhase) {
    case 'running':
      return { label: 'L1-L3 探测与推理中', tone: 'running' }
    case 'awaiting_approval':
      return { label: 'LangGraph 中断 · 待确认', tone: 'interrupt' }
    case 'executed':
      return { label: '已事务执行', tone: 'success' }
    case 'cancelled':
      return { label: '已取消执行', tone: 'muted' }
    case 'error':
      return { label: '遇到异常', tone: 'danger' }
    default:
      return { label: '就绪', tone: 'muted' }
  }
})

watch(
  () => [
    store.traceItems.length,
    store.traceItems[store.traceItems.length - 1]?.summary?.length ?? 0,
  ],
  async () => {
    await nextTick()
    if (scrollContainer.value) {
      scrollContainer.value.scrollTop = scrollContainer.value.scrollHeight
    }
  },
)

function toggleExpand(eventId: string) {
  expandedEventIds[eventId] = !expandedEventIds[eventId]
}

function handleSubmit() {
  const text = promptInput.value.trim()
  if (!text || store.isStreaming || store.isExecuting) return
  promptInput.value = ''
  store.startAgentRun(text)
}

async function handleNewChat() {
  if (store.isExecuting) return
  const hadContext = Boolean(
    store.currentThreadId || store.traceItems.length > 0 || store.pendingPlan,
  )
  store.startNewChat()
  promptInput.value = ''
  Object.keys(expandedEventIds).forEach((k) => delete expandedEventIds[k])
  await nextTick()
  textareaRef.value?.focus()
  if (hadContext) {
    ElMessage.info('已开启新对话，当前会话上下文与待审提案已清空')
  }
}

function handleKeydown(e: KeyboardEvent) {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
    e.preventDefault()
    handleSubmit()
  }
}

function applyQuickPrompt(text: string) {
  promptInput.value = text
}

function formatTime(iso: string): string {
  try {
    const d = new Date(iso)
    return d.toLocaleTimeString('zh-CN', { hour12: false })
  } catch {
    return ''
  }
}

function getPreviewSnippet(item: DisplayTraceItem): string | null {
  const detail = item.endDetail as Record<string, unknown> | undefined
  if (!detail) return null
  if (typeof detail.preview === 'string' && detail.preview.trim()) {
    return detail.preview
  }
  if (typeof detail.error === 'string' && detail.error.trim()) {
    return detail.error
  }
  return null
}

function getToolTargetPath(item: DisplayTraceItem): string {
  const args = item.tool_args as Record<string, unknown> | undefined
  if (!args) return ''
  if (typeof args.file_path === 'string') return args.file_path
  if (typeof args.subpath === 'string') return args.subpath
  if (typeof args.operation_count === 'number') return `${args.operation_count} 项拟定操作`
  return ''
}

function getProbeLabel(level?: string | null): string {
  if (level === 'L1') return 'L1 目录扫描'
  if (level === 'L2') return 'L2 轻量探针'
  if (level === 'L3') return 'L3 深度读取'
  if (level === 'PLAN') return 'PLAN 提案'
  return 'TOOL'
}
</script>

<template>
  <aside class="trace-panel">
    <!-- Top Session Toolbar with New Chat CTA -->
    <div class="session-toolbar">
      <div class="session-info">
        <span class="session-heading">推理轨迹与对话</span>
        <span
          v-if="store.currentThreadId"
          class="thread-pill mono"
          :title="`当前会话 ID: ${store.currentThreadId}（支持多轮记忆与指令微调）`"
        >
          <span class="thread-dot" />
          <span>第 {{ Math.max(1, store.conversationTurnCount) }} 轮</span>
        </span>
      </div>

      <el-tooltip content="开启新会话并清空上下文" placement="bottom">
        <button
          type="button"
          class="btn-new-chat"
          :disabled="store.isExecuting"
          @click="handleNewChat"
        >
          <MessageSquarePlus :size="13" />
          <span>+ 新增对话</span>
        </button>
      </el-tooltip>
    </div>

    <!-- Instruction Composer -->
    <section class="composer-card">
      <div class="composer-header">
        <span class="composer-title">
          {{ store.currentThreadId ? '继续对话 / 微调指令' : '整理指令' }}
        </span>
        <span class="composer-hint mono">Ctrl + Enter 发送</span>
      </div>

      <textarea
        ref="textareaRef"
        v-model="promptInput"
        class="composer-textarea"
        rows="3"
        :disabled="store.isStreaming || store.isExecuting"
        :placeholder="
          store.currentThreadId
            ? '继续追问或细化整理要求，例如：把旧实验报告移到归档目录，发票按年月前缀重命名...'
            : '描述您的目录整理需求，例如：按项目、财务发票与代码自动分类，将模糊文件名规范重命名...'
        "
        @keydown="handleKeydown"
      />

      <div class="preset-chips">
        <button
          v-for="(preset, idx) in quickPresets"
          :key="idx"
          type="button"
          class="quick-chip"
          :disabled="store.isStreaming"
          @click="applyQuickPrompt(preset)"
        >
          {{ preset.slice(0, 18) }}…
        </button>
      </div>

      <div class="composer-footer">
        <div v-if="!store.isModelReady" class="key-warning" @click="store.configDialogOpen = true">
          <AlertTriangle :size="13" />
          <span>未配置 BYOK API Key，点击配置</span>
        </div>
        <div v-else class="safe-caption">
          <span>
            {{
              store.currentThreadId
                ? '已延续当前会话记忆 · 仅触发只读探针 + Dry-Run 提案'
                : '仅触发只读探针 + Dry-Run 提案'
            }}
          </span>
        </div>

        <div class="composer-actions">
          <button
            v-if="store.isStreaming"
            type="button"
            class="btn-stop"
            @click="store.abortAgentRun()"
          >
            <Square :size="12" />
            <span>停止</span>
          </button>
          <button
            v-else
            type="button"
            class="btn-run"
            :disabled="!promptInput.trim() || store.isExecuting"
            @click="handleSubmit"
          >
            <span>{{ store.currentThreadId ? '继续对话' : '开始分析' }}</span>
            <CornerDownLeft :size="13" />
          </button>
        </div>
      </div>
    </section>

    <!-- Progressive Disclosure Telemetry Bar -->
    <div class="telemetry-bar">
      <div class="telemetry-left">
        <span class="status-pill" :class="phaseBadge.tone">
          <Loader2 v-if="store.isStreaming" :size="11" class="spinning" />
          <span>{{ phaseBadge.label }}</span>
        </span>
      </div>

      <div class="telemetry-counters mono">
        <span class="counter-chip l1" title="L1 元数据目录树扫描次数">
          L1 {{ store.probeStats.L1 }}
        </span>
        <span class="counter-chip l2" title="L2 头部/PDF首页轻量探针次数">
          L2 {{ store.probeStats.L2 }}
        </span>
        <span class="counter-chip l3" title="L3 深度内容读取次数">
          L3 {{ store.probeStats.L3 }}
        </span>
        <span
          v-if="store.probeStats.blocked > 0"
          class="counter-chip blocked"
          title="SandboxGuard 越权拦截次数"
        >
          拦截 {{ store.probeStats.blocked }}
        </span>
        <button
          v-if="store.traceItems.length > 0 && !store.isStreaming"
          type="button"
          class="clear-btn"
          title="清空轨迹日志"
          @click="store.clearTrace()"
        >
          <Trash2 :size="12" />
        </button>
      </div>
    </div>

    <!-- Real-time Trace Timeline -->
    <div ref="scrollContainer" class="trace-stream">
      <!-- Empty Onboarding State -->
      <div v-if="store.traceItems.length === 0" class="empty-trace">
        <div class="empty-icon">
          <Layers :size="20" />
        </div>
        <h4>渐进式披露轨迹流 (L1 → L2 → L3)</h4>
        <p>
          Agent 将优先调用 <strong>L1</strong> 扫描目录元数据，仅对模糊文件按需触发
          <strong>L2</strong> 轻量头部/PDF首页探针与 <strong>L3</strong> 深度读取，最终通过
          <strong>LangGraph Interrupt</strong> 挂起并交由右侧 Diff 审批。
        </p>
        <div class="legend-grid">
          <div class="legend-item">
            <span class="probe-badge l1">L1</span>
            <span>树结构与文件元数据</span>
          </div>
          <div class="legend-item">
            <span class="probe-badge l2">L2</span>
            <span>头部行 / PDF 首页</span>
          </div>
          <div class="legend-item">
            <span class="probe-badge l3">L3</span>
            <span>深度切片消歧</span>
          </div>
          <div class="legend-item">
            <span class="probe-badge plan">PLAN</span>
            <span>Dry-Run 中断提案</span>
          </div>
        </div>
      </div>

      <!-- Trace Event Bubbles -->
      <div v-for="item in store.traceItems" :key="item.event_id" class="trace-row">
        <!-- User Message Bubble (Multi-Turn Conversation) -->
        <div v-if="item.type === 'user_message'" class="user-bubble">
          <div class="bubble-head">
            <span class="user-tag">
              <User :size="11" />
              <span>你</span>
            </span>
            <span class="meta-time mono">{{ formatTime(item.timestamp) }}</span>
          </div>
          <p class="user-text">{{ item.summary }}</p>
        </div>

        <!-- Run Started -->
        <div v-else-if="item.type === 'run_started'" class="meta-divider">
          <span class="meta-text">{{ item.summary }}</span>
          <span class="meta-time mono">{{ formatTime(item.timestamp) }}</span>
        </div>

        <!-- Thinking / Reasoning Stream -->
        <div v-else-if="item.type === 'thinking'" class="thinking-bubble">
          <div class="bubble-head">
            <span class="thinking-tag">
              <Brain :size="12" />
              <span>思考流</span>
            </span>
            <span class="meta-time mono">{{ formatTime(item.timestamp) }}</span>
          </div>
          <p class="thinking-text">{{ item.summary }}</p>
        </div>

        <!-- Progressive Disclosure Tool Probe Card (L1 / L2 / L3 / PLAN) -->
        <div
          v-else-if="item.type === 'tool_start' || item.type === 'tool_end'"
          class="probe-card"
          :class="[
            (item.probe_level || 'l1').toLowerCase(),
            { blocked: item.blocked },
          ]"
        >
          <div class="probe-head" @click="toggleExpand(item.event_id)">
            <div class="probe-head-left">
              <span class="probe-badge" :class="(item.probe_level || 'l1').toLowerCase()">
                <FolderSearch v-if="item.probe_level === 'L1'" :size="11" />
                <Eye v-else-if="item.probe_level === 'L2'" :size="11" />
                <FileSearch v-else-if="item.probe_level === 'L3'" :size="11" />
                <Layers v-else :size="11" />
                <span>{{ getProbeLabel(item.probe_level) }}</span>
              </span>
              <span v-if="getToolTargetPath(item)" class="probe-target mono">
                {{ getToolTargetPath(item) }}
              </span>
            </div>

            <div class="probe-head-right">
              <Loader2 v-if="!item.completed" :size="13" class="spinning icon-muted" />
              <ShieldAlert v-else-if="item.blocked" :size="13" class="icon-danger" />
              <CheckCircle2 v-else :size="13" class="icon-success" />
              <component
                :is="expandedEventIds[item.event_id] ? ChevronDown : ChevronRight"
                v-if="getPreviewSnippet(item)"
                :size="13"
                class="icon-muted"
              />
            </div>
          </div>

          <div class="probe-summary">
            {{ item.endSummary || item.summary }}
          </div>

          <pre
            v-if="expandedEventIds[item.event_id] && getPreviewSnippet(item)"
            class="probe-preview mono"
          >{{ getPreviewSnippet(item) }}</pre>
        </div>

        <!-- SandboxGuard Security Block Alert -->
        <div v-else-if="item.type === 'security_blocked'" class="security-alert">
          <div class="alert-head">
            <ShieldAlert :size="14" />
            <span>SandboxGuard 路径越权已拦截</span>
            <span class="meta-time mono">{{ formatTime(item.timestamp) }}</span>
          </div>
          <p class="alert-text">{{ item.summary }}</p>
        </div>

        <!-- LangGraph Interrupt Checkpoint -->
        <div v-else-if="item.type === 'plan_interrupt'" class="interrupt-card">
          <div class="interrupt-head">
            <PauseCircle :size="15" />
            <span>LangGraph 状态机已挂起 (Interrupt)</span>
          </div>
          <p class="interrupt-text">
            {{ item.summary }}
          </p>
        </div>

        <!-- Execution Complete / Undo Confirmation -->
        <div v-else-if="item.type === 'execution_complete'" class="complete-card">
          <CheckCircle2 :size="14" />
          <span>{{ item.summary }}</span>
        </div>

        <!-- Assistant Message -->
        <div v-else-if="item.type === 'assistant_message'" class="assistant-bubble">
          <div class="bubble-head">
            <span class="assistant-tag">
              <Bot :size="12" />
              <span>SafeWorkspace Agent</span>
            </span>
            <span class="meta-time mono">{{ formatTime(item.timestamp) }}</span>
          </div>
          <p>{{ item.summary }}</p>
        </div>

        <!-- Error Card -->
        <div v-else-if="item.type === 'error'" class="error-card">
          <div class="alert-head">
            <AlertTriangle :size="14" />
            <span>执行异常</span>
          </div>
          <p class="alert-text">{{ item.summary }}</p>
          <button
            v-if="item.summary.includes('API Key') || item.summary.includes('401')"
            type="button"
            class="inline-cfg-btn"
            @click="store.configDialogOpen = true"
          >
            <Settings :size="12" />
            <span>打开 BYOK 模型配置</span>
          </button>
        </div>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.trace-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background-color: var(--bg-surface);
  border-right: 1px solid var(--border-default);
}

/* Session Top Toolbar */
.session-toolbar {
  padding: 9px 16px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-surface);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-shrink: 0;
}

.session-info {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.session-heading {
  font-size: 12.5px;
  font-weight: 650;
  color: var(--text-primary);
  letter-spacing: -0.01em;
}

.thread-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 10.5px;
  font-weight: 550;
  padding: 1px 7px;
  border-radius: 999px;
  background: var(--l1-bg);
  color: var(--l1-text);
  border: 1px solid var(--l1-border);
}

.thread-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--l1-text);
}

.btn-new-chat {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 27px;
  padding: 0 10px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-strong);
  background: var(--bg-surface);
  color: var(--text-primary);
  font-size: 11.5px;
  font-weight: 550;
  cursor: pointer;
  transition: all 0.14s ease;
  flex-shrink: 0;
}

.btn-new-chat:hover:not(:disabled) {
  background: var(--bg-hover);
  border-color: var(--text-primary);
}

.btn-new-chat:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

/* Composer */
.composer-card {
  padding: 11px 16px 12px;
  border-bottom: 1px solid var(--border-default);
  display: flex;
  flex-direction: column;
  gap: 8px;
  background-color: var(--bg-surface);
}

.composer-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.composer-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  letter-spacing: 0.01em;
}

.composer-hint {
  font-size: 10.5px;
  color: var(--text-faint);
}

.composer-textarea {
  width: 100%;
  resize: none;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  background: var(--bg-canvas);
  color: var(--text-primary);
  font-family: var(--font-sans);
  font-size: 13px;
  line-height: 1.5;
  padding: 9px 11px;
  outline: none;
  transition: border-color 0.15s ease, background-color 0.15s ease;
}

.composer-textarea:focus {
  border-color: var(--text-primary);
  background: var(--bg-surface);
}

.composer-textarea::placeholder {
  color: var(--text-faint);
}

.preset-chips {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.quick-chip {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  color: var(--text-muted);
  cursor: pointer;
  transition: all 0.12s ease;
}

.quick-chip:hover:not(:disabled) {
  border-color: var(--border-strong);
  color: var(--text-primary);
  background: var(--bg-hover);
}

.composer-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 2px;
}

.safe-caption {
  font-size: 11px;
  color: var(--text-muted);
}

.key-warning {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11.5px;
  font-weight: 500;
  color: var(--l3-text);
  cursor: pointer;
}

.key-warning:hover {
  text-decoration: underline;
}

.btn-run,
.btn-stop {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 30px;
  padding: 0 12px;
  border-radius: var(--radius-md);
  font-size: 12px;
  font-weight: 550;
  border: 1px solid transparent;
  cursor: pointer;
  transition: all 0.15s ease;
}

.btn-run {
  background: var(--cta-bg);
  color: var(--cta-text);
}

.btn-run:hover:not(:disabled) {
  background: var(--cta-bg-hover);
}

.btn-run:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.btn-stop {
  background: var(--danger-bg);
  color: var(--danger-text);
  border-color: var(--danger-border);
}

/* Telemetry Strip */
.telemetry-bar {
  padding: 8px 16px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-canvas);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-shrink: 0;
}

.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--border-default);
}

.status-pill.muted {
  color: var(--text-muted);
  background: var(--bg-surface);
}

.status-pill.running {
  color: var(--l1-text);
  background: var(--l1-bg);
  border-color: var(--l1-border);
}

.status-pill.interrupt {
  color: var(--l3-text);
  background: var(--l3-bg);
  border-color: var(--l3-border);
}

.status-pill.success {
  color: var(--plan-text);
  background: var(--plan-bg);
  border-color: var(--plan-border);
}

.status-pill.danger {
  color: var(--danger-text);
  background: var(--danger-bg);
  border-color: var(--danger-border);
}

.telemetry-counters {
  display: flex;
  align-items: center;
  gap: 5px;
}

.counter-chip {
  font-size: 10.5px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 5px;
  border: 1px solid transparent;
}

.counter-chip.l1 {
  color: var(--l1-text);
  background: var(--l1-bg);
  border-color: var(--l1-border);
}

.counter-chip.l2 {
  color: var(--l2-text);
  background: var(--l2-bg);
  border-color: var(--l2-border);
}

.counter-chip.l3 {
  color: var(--l3-text);
  background: var(--l3-bg);
  border-color: var(--l3-border);
}

.counter-chip.blocked {
  color: var(--danger-text);
  background: var(--danger-bg);
  border-color: var(--danger-border);
}

.clear-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: 5px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.clear-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

/* Trace Timeline */
.trace-stream {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 14px 16px 24px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.empty-trace {
  margin: auto 0;
  padding: 20px 14px;
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
}

.empty-icon {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  border: 1px solid var(--border-default);
  background: var(--bg-canvas);
  color: var(--text-secondary);
  display: flex;
  align-items: center;
  justify-content: center;
}

.empty-trace h4 {
  margin: 0;
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text-primary);
}

.empty-trace p {
  margin: 0;
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.65;
  max-width: 340px;
}

.legend-grid {
  margin-top: 6px;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  width: 100%;
  max-width: 330px;
}

.legend-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-subtle);
  background: var(--bg-canvas);
  font-size: 11px;
  color: var(--text-secondary);
}

.meta-divider {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 11px;
  color: var(--text-muted);
  padding: 2px 2px 4px;
  border-bottom: 1px dashed var(--border-subtle);
}

.user-bubble {
  padding: 9px 12px;
  border-radius: var(--radius-md);
  background: var(--bg-subtle);
  border: 1px solid var(--border-default);
}

.user-tag {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-primary);
}

.user-text {
  margin: 0;
  font-size: 12.5px;
  color: var(--text-primary);
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.55;
}

.assistant-tag {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  font-weight: 600;
  color: var(--plan-text);
}

.meta-time {
  font-size: 10.5px;
  color: var(--text-faint);
}

.thinking-bubble {
  padding: 9px 11px;
  border-radius: var(--radius-md);
  background: var(--bg-canvas);
  border: 1px solid var(--border-subtle);
}

.bubble-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 4px;
}

.thinking-tag {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-muted);
}

.thinking-text {
  margin: 0;
  font-size: 12.5px;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.55;
}

.probe-card {
  border-radius: var(--radius-md);
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  overflow: hidden;
  box-shadow: var(--shadow-xs);
}

.probe-card.blocked {
  border-color: var(--danger-border);
}

.probe-head {
  padding: 7px 10px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  cursor: pointer;
  background: var(--bg-canvas);
  border-bottom: 1px solid var(--border-subtle);
}

.probe-head-left {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}

.probe-head-right {
  display: flex;
  align-items: center;
  gap: 5px;
  flex-shrink: 0;
}

.probe-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 6px;
  border-radius: 5px;
  font-size: 10.5px;
  font-weight: 600;
  border: 1px solid transparent;
  flex-shrink: 0;
}

.probe-badge.l1 {
  color: var(--l1-text);
  background: var(--l1-bg);
  border-color: var(--l1-border);
}

.probe-badge.l2 {
  color: var(--l2-text);
  background: var(--l2-bg);
  border-color: var(--l2-border);
}

.probe-badge.l3 {
  color: var(--l3-text);
  background: var(--l3-bg);
  border-color: var(--l3-border);
}

.probe-badge.plan {
  color: var(--plan-text);
  background: var(--plan-bg);
  border-color: var(--plan-border);
}

.probe-target {
  font-size: 11.5px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.probe-summary {
  padding: 7px 10px;
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.45;
}

.probe-preview {
  margin: 0;
  padding: 8px 10px;
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-subtle);
  color: var(--text-secondary);
  font-size: 11px;
  line-height: 1.45;
  max-height: 170px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
}

.security-alert,
.error-card {
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--danger-bg);
  border: 1px solid var(--danger-border);
  color: var(--danger-text);
}

.alert-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 4px;
}

.alert-text {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  word-break: break-word;
}

.inline-cfg-btn {
  margin-top: 8px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 9px;
  border-radius: 6px;
  border: 1px solid var(--danger-border);
  background: var(--bg-surface);
  color: var(--text-primary);
  font-size: 11.5px;
  font-weight: 500;
  cursor: pointer;
}

.interrupt-card {
  padding: 11px 12px;
  border-radius: var(--radius-md);
  background: var(--plan-bg);
  border: 1px solid var(--plan-border);
  color: var(--plan-text);
}

.interrupt-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  font-weight: 650;
  margin-bottom: 4px;
}

.interrupt-text {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
}

.complete-card {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  padding: 9px 11px;
  border-radius: var(--radius-md);
  background: var(--plan-bg);
  border: 1px solid var(--plan-border);
  color: var(--plan-text);
  font-size: 12px;
  line-height: 1.5;
}

.assistant-bubble {
  padding: 10px 12px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  color: var(--text-primary);
  font-size: 12.5px;
  line-height: 1.6;
}

.assistant-bubble p {
  margin: 0;
  white-space: pre-wrap;
}

.icon-muted {
  color: var(--text-muted);
}

.icon-success {
  color: #10b981;
}

.icon-danger {
  color: var(--danger-text);
}

.spinning {
  animation: spin 0.9s linear infinite;
}

@keyframes spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}
</style>
