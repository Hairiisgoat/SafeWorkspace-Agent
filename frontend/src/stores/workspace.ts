import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import {
  apiClient,
  type ExecutionBatchRecord,
  type FileOperation,
  type FileTreeNode,
  type OrganizationPlan,
  type TraceEvent,
  type WorkspaceConfigResponse,
  type WorkspaceConfigUpdateRequest,
} from '@/api/client'

const PROTECTED_SEGMENTS = new Set(['.agent_history', '.git'])
const RESERVED_WIN_NAMES = new Set([
  'con',
  'prn',
  'aux',
  'nul',
  'com1',
  'com2',
  'com3',
  'com4',
  'com5',
  'com6',
  'com7',
  'com8',
  'com9',
  'lpt1',
  'lpt2',
  'lpt3',
  'lpt4',
  'lpt5',
  'lpt6',
  'lpt7',
  'lpt8',
  'lpt9',
])
const ILLEGAL_SEGMENT_RE = /[<>:"|?*\x00]/

export function normalizeRelativePosix(raw: string): string {
  const cleaned = (raw || '').replace(/\\/g, '/').trim()
  if (!cleaned) {
    throw new Error('路径不能为空')
  }
  if (/^[a-zA-Z]:/.test(cleaned) || cleaned.startsWith('//')) {
    throw new Error('仅允许使用沙盒内的相对路径，禁止绝对路径或盘符路径')
  }
  const stripped = cleaned.replace(/^(\.\/)+/, '').replace(/^\/+|\/+$/g, '')
  if (!stripped) {
    throw new Error('目标路径不能为空')
  }
  const segments = stripped.split('/').filter((s) => s.length > 0)
  for (const seg of segments) {
    if (seg === '.' || seg === '..') {
      throw new Error('路径中禁止包含 "." 或 ".." 越权片段')
    }
    if (ILLEGAL_SEGMENT_RE.test(seg)) {
      throw new Error(`路径片段 "${seg}" 包含非法字符 (< > : " | ? *)`)
    }
    if (seg.endsWith(' ') || seg.endsWith('.')) {
      throw new Error(`路径片段 "${seg}" 不能以空格或点号结尾`)
    }
    if (PROTECTED_SEGMENTS.has(seg.toLowerCase())) {
      throw new Error(`禁止操作受保护系统目录 "${seg}"`)
    }
    const stem = seg.split('.')[0].trim().toLowerCase()
    if (RESERVED_WIN_NAMES.has(stem)) {
      throw new Error(`禁止使用操作系统保留文件名 "${seg}"`)
    }
  }
  return segments.join('/')
}

export function validateSingleSegmentName(rawName: string): string {
  const trimmed = (rawName || '').trim()
  if (!trimmed) {
    throw new Error('名称不能为空')
  }
  if (trimmed.includes('/') || trimmed.includes('\\')) {
    throw new Error('重命名仅支持修改当前名称，不能包含路径分隔符 "/" 或 "\\"')
  }
  return normalizeRelativePosix(trimmed)
}

function parentDirOf(relPosix: string): string {
  const idx = relPosix.lastIndexOf('/')
  return idx === -1 ? '.' : relPosix.slice(0, idx)
}

function cloneOperations(ops: FileOperation[]): FileOperation[] {
  return ops.map((op) => ({ ...op }))
}

export type AgentPhase =
  | 'idle'
  | 'running'
  | 'awaiting_approval'
  | 'executed'
  | 'cancelled'
  | 'error'

export interface DisplayTraceItem extends TraceEvent {
  /** Merged result summary when tool_start and tool_end are paired cleanly in UI */
  endSummary?: string
  endDetail?: unknown
  completed?: boolean
  blocked?: boolean
}

export const useWorkspaceStore = defineStore('workspace', () => {
  // Core workspace & model configuration
  const config = ref<WorkspaceConfigResponse | null>(null)
  const liveTree = ref<FileTreeNode | null>(null)
  const historyBatches = ref<ExecutionBatchRecord[]>([])

  // Runtime state
  const isLoading = ref(false)
  const isStreaming = ref(false)
  const isExecuting = ref(false)
  const isSeeding = ref(false)
  const isPickingDirectory = ref(false)
  const isUndoingId = ref<string | null>(null)
  const configDialogOpen = ref(false)
  const workspacePanelOpen = ref(false)
  const darkMode = ref(false)
  const activeRightTab = ref<'diff' | 'history'>('diff')

  // Active agent session & HITL interrupt state
  const agentPhase = ref<AgentPhase>('idle')
  const currentThreadId = ref<string | null>(null)
  const lastInstruction = ref('')
  const traceItems = ref<DisplayTraceItem[]>([])
  const pendingPlan = ref<OrganizationPlan | null>(null)
  const originalPlanOperations = ref<FileOperation[]>([])
  const planEdited = ref(false)
  const manualOpSeq = ref(1)
  const selectedOperationIds = ref<string[]>([])
  const highlightedOperationId = ref<string | null>(null)
  const lastBatchResult = ref<ExecutionBatchRecord | null>(null)
  const lastStatusMessage = ref<string>('')

  let abortController: AbortController | null = null

  // -------------------------------------------------------------------------
  // Telemetry & Computed Selectors
  // -------------------------------------------------------------------------

  const probeStats = computed(() => {
    let L1 = 0
    let L2 = 0
    let L3 = 0
    let PLAN = 0
    let blocked = 0

    for (const item of traceItems.value) {
      if (item.type === 'tool_start') {
        if (item.probe_level === 'L1') L1++
        else if (item.probe_level === 'L2') L2++
        else if (item.probe_level === 'L3') L3++
        else if (item.probe_level === 'PLAN') PLAN++
      } else if (item.type === 'security_blocked') {
        blocked++
      }
    }
    return { L1, L2, L3, PLAN, blocked }
  })

  const selectedCount = computed(() => selectedOperationIds.value.length)
  const totalPlanOps = computed(() => pendingPlan.value?.operations.length || 0)
  const conversationTurnCount = computed(
    () => traceItems.value.filter((item) => item.type === 'user_message').length,
  )
  const manualAdjustedCount = computed(
    () => (pendingPlan.value?.operations || []).filter((op) => op.manual_adjusted).length,
  )
  const hasManualEdits = computed(
    () => planEdited.value || manualAdjustedCount.value > 0,
  )
  const undoableBatchCount = computed(
    () => historyBatches.value.filter((b) => b.status === 'executed').length,
  )

  const isLocalEndpoint = computed(() => {
    const url = (config.value?.base_url || '').toLowerCase()
    return (
      url.includes('localhost') ||
      url.includes('127.0.0.1') ||
      url.includes('0.0.0.0')
    )
  })

  const isModelReady = computed(
    () => Boolean(config.value?.has_api_key) || isLocalEndpoint.value,
  )

  // -------------------------------------------------------------------------
  // Workspace Actions
  // -------------------------------------------------------------------------

  function applyTheme(dark: boolean) {
    darkMode.value = dark
    document.documentElement.classList.toggle('dark', dark)
    try {
      localStorage.setItem('sw_theme', dark ? 'dark' : 'light')
    } catch {
      // ignore storage failure
    }
  }

  function initTheme() {
    try {
      const saved = localStorage.getItem('sw_theme')
      if (saved === 'dark') {
        applyTheme(true)
        return
      }
    } catch {
      // ignore
    }
    applyTheme(false)
  }

  function toggleDarkMode() {
    applyTheme(!darkMode.value)
  }

  async function initialize() {
    initTheme()
    isLoading.value = true
    try {
      const [cfg, tree, hist] = await Promise.all([
        apiClient.getConfig(),
        apiClient.getWorkspaceTree(),
        apiClient.getHistory(),
      ])
      config.value = cfg
      liveTree.value = tree
      historyBatches.value = hist
    } finally {
      isLoading.value = false
    }
  }

  async function refreshWorkspace() {
    const [cfg, tree, hist] = await Promise.all([
      apiClient.getConfig(),
      apiClient.getWorkspaceTree(),
      apiClient.getHistory(),
    ])
    config.value = cfg
    liveTree.value = tree
    historyBatches.value = hist
  }

  function resetSessionState() {
    if (abortController) {
      abortController.abort()
      abortController = null
    }
    isStreaming.value = false
    currentThreadId.value = null
    lastInstruction.value = ''
    traceItems.value = []
    pendingPlan.value = null
    originalPlanOperations.value = []
    planEdited.value = false
    manualOpSeq.value = 1
    selectedOperationIds.value = []
    highlightedOperationId.value = null
    lastBatchResult.value = null
    lastStatusMessage.value = ''
    agentPhase.value = 'idle'
  }

  function startNewChat() {
    resetSessionState()
  }

  async function saveConfig(payload: WorkspaceConfigUpdateRequest) {
    const prevRoot = config.value?.sandbox_root || ''
    const updated = await apiClient.updateConfig(payload)
    config.value = updated

    // Switching SANDBOX_ROOT silently starts a fresh conversation so old directory context cannot leak
    if (prevRoot && updated.sandbox_root !== prevRoot) {
      resetSessionState()
    }

    // Refresh live tree & history in case SANDBOX_ROOT changed
    const [tree, hist] = await Promise.all([
      apiClient.getWorkspaceTree(),
      apiClient.getHistory(),
    ])
    liveTree.value = tree
    historyBatches.value = hist
    return updated
  }

  async function selectWorkspaceViaDialog() {
    isPickingDirectory.value = true
    try {
      return await apiClient.selectWorkspaceDialog()
    } finally {
      isPickingDirectory.value = false
    }
  }

  async function switchWorkspace(targetPath: string) {
    return await saveConfig({ sandbox_root: targetPath })
  }

  async function seedDemoWorkspace() {
    isSeeding.value = true
    try {
      const res = await apiClient.seedDemoWorkspace()
      liveTree.value = res.updated_tree
      config.value = await apiClient.getConfig()
      return res
    } finally {
      isSeeding.value = false
    }
  }

  // -------------------------------------------------------------------------
  // SSE Trace Ingestion & Smart Coalescing
  // -------------------------------------------------------------------------

  function ingestTraceEvent(evt: TraceEvent) {
    if (evt.thread_id) {
      currentThreadId.value = evt.thread_id
      // Backfill thread_id on any just-submitted local user_message awaiting server-assigned thread_id
      for (let i = traceItems.value.length - 1; i >= 0; i--) {
        if (traceItems.value[i].thread_id === 'pending') {
          traceItems.value[i].thread_id = evt.thread_id
        } else {
          break
        }
      }
    }

    // Coalesce consecutive streaming `thinking` chunks of the same kind into a single smooth bubble
    if (evt.type === 'thinking') {
      const last = traceItems.value[traceItems.value.length - 1]
      const lastKind = (last?.detail as Record<string, unknown> | undefined)?.kind
      const nextKind = (evt.detail as Record<string, unknown> | undefined)?.kind
      if (last && last.type === 'thinking' && lastKind === nextKind) {
        last.summary += evt.summary
        last.timestamp = evt.timestamp
        return
      }
      traceItems.value.push({ ...evt })
      return
    }

    // Smoothly upgrade a streamed `content_delta` thinking bubble into the final `assistant_message` bubble (no duplication)
    if (evt.type === 'assistant_message') {
      const last = traceItems.value[traceItems.value.length - 1]
      const lastKind = (last?.detail as Record<string, unknown> | undefined)?.kind
      if (last && last.type === 'thinking' && lastKind === 'content_delta') {
        last.type = 'assistant_message'
        last.summary = evt.summary
        last.timestamp = evt.timestamp
        last.detail = evt.detail
        return
      }
      traceItems.value.push({ ...evt })
      return
    }

    // Pair `tool_end` back onto the most recent open `tool_start` of the same tool
    if (evt.type === 'tool_end') {
      for (let i = traceItems.value.length - 1; i >= 0; i--) {
        const candidate = traceItems.value[i]
        if (
          candidate.type === 'tool_start' &&
          candidate.tool_name === evt.tool_name &&
          !candidate.completed
        ) {
          candidate.completed = true
          candidate.endSummary = evt.summary
          candidate.endDetail = evt.detail
          return
        }
      }
      traceItems.value.push({ ...evt, completed: true })
      return
    }

    if (evt.type === 'security_blocked') {
      // Also mark open matching tool_start as blocked
      for (let i = traceItems.value.length - 1; i >= 0; i--) {
        const candidate = traceItems.value[i]
        if (
          candidate.type === 'tool_start' &&
          candidate.tool_name === evt.tool_name &&
          !candidate.completed
        ) {
          candidate.completed = true
          candidate.blocked = true
          break
        }
      }
      traceItems.value.push({ ...evt, blocked: true })
      return
    }

    if (evt.type === 'plan_interrupt') {
      const plan = evt.detail as OrganizationPlan | null
      if (plan && Array.isArray(plan.operations)) {
        pendingPlan.value = {
          ...plan,
          operations: cloneOperations(plan.operations),
        }
        originalPlanOperations.value = cloneOperations(plan.operations)
        planEdited.value = false
        manualOpSeq.value = 1
        selectedOperationIds.value = plan.operations.map((op) => op.id)
        agentPhase.value = 'awaiting_approval'
        activeRightTab.value = 'diff'
      }
      traceItems.value.push({ ...evt })
      return
    }

    if (evt.type === 'error') {
      agentPhase.value = 'error'
      lastStatusMessage.value = evt.summary
      traceItems.value.push({ ...evt })
      return
    }

    if (evt.type === 'done') {
      const detail = evt.detail as { awaiting_approval?: boolean } | undefined
      if (!detail?.awaiting_approval && agentPhase.value === 'running') {
        agentPhase.value = 'idle'
      }
      return
    }

    traceItems.value.push({ ...evt })
  }

  async function startAgentRun(instruction: string) {
    const trimmed = instruction.trim()
    if (!trimmed) return

    if (abortController) {
      abortController.abort()
    }
    abortController = new AbortController()

    lastInstruction.value = trimmed

    // Append user bubble to existing multi-turn conversation timeline (preserving prior turns)
    traceItems.value.push({
      event_id: `user_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
      thread_id: currentThreadId.value || 'pending',
      timestamp: new Date().toISOString(),
      type: 'user_message',
      summary: trimmed,
    })

    // Clear any stale unconfirmed plan if the user is refining instructions via follow-up chat
    pendingPlan.value = null
    originalPlanOperations.value = []
    planEdited.value = false
    selectedOperationIds.value = []
    highlightedOperationId.value = null
    lastBatchResult.value = null
    lastStatusMessage.value = ''
    isStreaming.value = true
    agentPhase.value = 'running'
    activeRightTab.value = 'diff'

    try {
      await apiClient.streamAgentRun(
        {
          instruction: trimmed,
          thread_id: currentThreadId.value,
        },
        {
          onEvent: (evt) => ingestTraceEvent(evt),
          onError: (err) => {
            ingestTraceEvent({
              event_id: `local_err_${Date.now()}`,
              thread_id: currentThreadId.value || 'local',
              timestamp: new Date().toISOString(),
              type: 'error',
              summary: err.message,
            })
          },
        },
        abortController.signal,
      )
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') {
        agentPhase.value = 'cancelled'
        traceItems.value.push({
          event_id: `abort_${Date.now()}`,
          thread_id: currentThreadId.value || 'local',
          timestamp: new Date().toISOString(),
          type: 'assistant_message',
          summary: '已手动终止当前智能体分析流。',
        })
      } else {
        const msg = err instanceof Error ? err.message : String(err)
        agentPhase.value = 'error'
        lastStatusMessage.value = msg
        traceItems.value.push({
          event_id: `err_${Date.now()}`,
          thread_id: currentThreadId.value || 'local',
          timestamp: new Date().toISOString(),
          type: 'error',
          summary: msg,
        })
      }
    } finally {
      isStreaming.value = false
      abortController = null
    }
  }

  function abortAgentRun() {
    if (abortController) {
      abortController.abort()
    }
  }

  // -------------------------------------------------------------------------
  // Granular Operation Selection & Human-in-the-Loop Confirm / Undo
  // -------------------------------------------------------------------------

  function toggleOperation(opId: string) {
    const idx = selectedOperationIds.value.indexOf(opId)
    if (idx >= 0) {
      selectedOperationIds.value.splice(idx, 1)
    } else {
      selectedOperationIds.value.push(opId)
    }
  }

  function selectAllOperations() {
    if (!pendingPlan.value) return
    selectedOperationIds.value = pendingPlan.value.operations.map((op) => op.id)
  }

  function clearSelectedOperations() {
    selectedOperationIds.value = []
  }

  function nextManualOpId(): string {
    const seq = manualOpSeq.value++
    return `op_manual_${String(seq).padStart(3, '0')}`
  }

  function ensureOpSelected(opId: string) {
    if (!selectedOperationIds.value.includes(opId)) {
      selectedOperationIds.value.push(opId)
    }
  }

  function unselectOp(opId: string) {
    const idx = selectedOperationIds.value.indexOf(opId)
    if (idx >= 0) selectedOperationIds.value.splice(idx, 1)
  }

  function inferOpType(
    sourcePath: string,
    targetPath: string,
    prevType?: FileOperation['op_type'],
  ): FileOperation['op_type'] {
    if (parentDirOf(sourcePath) === parentDirOf(targetPath)) {
      return 'rename'
    }
    if (
      prevType === 'archive' &&
      /^(archive|归档)(\/|$)/i.test(targetPath)
    ) {
      return 'archive'
    }
    return 'move'
  }

  /**
   * Drag & Drop a file node in the After Tree into a target directory (or '.' for root).
   */
  function moveAfterFileToFolder(
    fileNode: FileTreeNode,
    targetDirPath: string,
    existingAfterPaths: Set<string>,
  ): { changed: boolean; message: string; opId?: string } {
    if (!pendingPlan.value) return { changed: false, message: '' }
    if (fileNode.type !== 'file') {
      throw new Error('仅支持拖拽文件节点到目标目录')
    }

    const normalizedDir =
      targetDirPath === '.' || !targetDirPath.trim()
        ? '.'
        : normalizeRelativePosix(targetDirPath)
    const fileName = validateSingleSegmentName(fileNode.name)
    const newTarget =
      normalizedDir === '.' ? fileName : normalizeRelativePosix(`${normalizedDir}/${fileName}`)

    if (newTarget === fileNode.path) {
      return { changed: false, message: '' }
    }

    if (
      newTarget.toLowerCase() !== fileNode.path.toLowerCase() &&
      existingAfterPaths.has(newTarget.toLowerCase())
    ) {
      throw new Error(`目标位置已存在同名项：${newTarget}`)
    }

    const ops = pendingPlan.value.operations

    // 1. Direct file operation currently placing this file at fileNode.path
    const directOp = ops.find(
      (o) =>
        o.op_type !== 'mkdir' &&
        o.id === fileNode.operation_id &&
        o.target === fileNode.path,
    )

    if (directOp && directOp.source) {
      // Dragged back to its original path -> cancel this operation!
      if (newTarget === directOp.source) {
        if (directOp.id.startsWith('op_manual_')) {
          const idx = ops.findIndex((o) => o.id === directOp.id)
          if (idx >= 0) ops.splice(idx, 1)
          unselectOp(directOp.id)
        } else {
          // Restore AI original target & uncheck so it cleanly cancels the move
          const origAiOp = originalPlanOperations.value.find((o) => o.id === directOp.id)
          if (origAiOp) {
            directOp.target = origAiOp.target
            directOp.op_type = origAiOp.op_type
          }
          directOp.manual_adjusted = false
          unselectOp(directOp.id)
        }
        planEdited.value = true
        return {
          changed: true,
          message: `已将 ${fileName} 拖回原路径，自动取消该操作`,
        }
      }

      directOp.target = newTarget
      directOp.op_type = inferOpType(directOp.source, newTarget, directOp.op_type)
      directOp.category = normalizedDir === '.' ? null : normalizedDir.split('/')[0]
      directOp.manual_adjusted = true
      ensureOpSelected(directOp.id)
      planEdited.value = true
      return {
        changed: true,
        opId: directOp.id,
        message: `已调整 ${fileName} → ${normalizedDir === '.' ? '根目录' : `${normalizedDir}/`}`,
      }
    }

    // 2. Check if an unchecked operation already exists for this exact source file
    const uncheckedBySource = ops.find(
      (o) => o.op_type !== 'mkdir' && o.source === fileNode.path,
    )
    if (uncheckedBySource && uncheckedBySource.source) {
      uncheckedBySource.target = newTarget
      uncheckedBySource.op_type = inferOpType(
        uncheckedBySource.source,
        newTarget,
        uncheckedBySource.op_type,
      )
      uncheckedBySource.category = normalizedDir === '.' ? null : normalizedDir.split('/')[0]
      uncheckedBySource.manual_adjusted = true
      ensureOpSelected(uncheckedBySource.id)
      planEdited.value = true
      return {
        changed: true,
        opId: uncheckedBySource.id,
        message: `已重新启用并调整 ${fileName} → ${normalizedDir === '.' ? '根目录' : `${normalizedDir}/`}`,
      }
    }

    // 3. Previously untouched file (or file inside a moved dir -> valid chained move)
    const newOp: FileOperation = {
      id: nextManualOpId(),
      op_type: inferOpType(fileNode.path, newTarget),
      source: fileNode.path,
      target: newTarget,
      reason: '用户在预览树中手动拖拽调整归类',
      probe_evidence: 'L1',
      category: normalizedDir === '.' ? null : normalizedDir.split('/')[0],
      size_bytes: fileNode.size || 0,
      manual_adjusted: true,
    }
    ops.push(newOp)
    ensureOpSelected(newOp.id)
    planEdited.value = true
    return {
      changed: true,
      opId: newOp.id,
      message: `已新增移动操作：${fileNode.path} → ${newTarget}`,
    }
  }

  /**
   * Create a new virtual target folder (op_type: 'mkdir') immediately inside the After Tree.
   */
  function createVirtualFolder(
    rawFolderPath: string,
    existingAfterPaths: Set<string>,
  ): FileOperation {
    if (!pendingPlan.value) {
      throw new Error('当前没有待审批的整理提案')
    }
    const normalized = normalizeRelativePosix(rawFolderPath)
    if (existingAfterPaths.has(normalized.toLowerCase())) {
      throw new Error(`目录 "${normalized}" 已存在于当前预览树中`)
    }

    const ops = pendingPlan.value.operations
    const existingMkdir = ops.find(
      (o) => o.op_type === 'mkdir' && o.target.toLowerCase() === normalized.toLowerCase(),
    )
    if (existingMkdir) {
      existingMkdir.manual_adjusted = true
      ensureOpSelected(existingMkdir.id)
      planEdited.value = true
      return existingMkdir
    }

    const newOp: FileOperation = {
      id: nextManualOpId(),
      op_type: 'mkdir',
      source: null,
      target: normalized,
      reason: '用户在预览树中手动新建目标目录',
      probe_evidence: 'L1',
      category: normalized.split('/')[0],
      size_bytes: 0,
      manual_adjusted: true,
    }

    // Keep mkdir operations grouped neatly near the top
    let insertIdx = 0
    while (insertIdx < ops.length && ops[insertIdx].op_type === 'mkdir') {
      insertIdx++
    }
    ops.splice(insertIdx, 0, newOp)
    ensureOpSelected(newOp.id)
    planEdited.value = true
    return newOp
  }

  /**
   * Inline double-click rename of any file node or newly-created virtual folder in After Tree.
   */
  function renameAfterNode(
    node: FileTreeNode,
    rawNewName: string,
    existingAfterPaths: Set<string>,
  ): { changed: boolean; message: string; opId?: string } {
    if (!pendingPlan.value) return { changed: false, message: '' }

    const cleanName = validateSingleSegmentName(rawNewName)
    const parent = parentDirOf(node.path)
    const newTarget =
      parent === '.' ? cleanName : normalizeRelativePosix(`${parent}/${cleanName}`)

    if (newTarget === node.path) {
      return { changed: false, message: '' }
    }

    if (
      newTarget.toLowerCase() !== node.path.toLowerCase() &&
      existingAfterPaths.has(newTarget.toLowerCase())
    ) {
      throw new Error(`同级目录下已存在同名项：${cleanName}`)
    }

    const ops = pendingPlan.value.operations

    // Folder rename (only for newly created virtual folders)
    if (node.type === 'dir') {
      if (node.diff_status !== 'added') {
        throw new Error('仅支持在预览树中重命名新建目标目录或文件')
      }
      const oldPrefix = `${node.path}/`
      let touchedId: string | undefined
      for (const op of ops) {
        if (op.target === node.path) {
          op.target = newTarget
          op.manual_adjusted = true
          touchedId = touchedId || op.id
        } else if (op.target.startsWith(oldPrefix)) {
          op.target = `${newTarget}/${op.target.slice(oldPrefix.length)}`
          op.manual_adjusted = true
          touchedId = touchedId || op.id
        }
        if (op.source === node.path) {
          op.source = newTarget
          op.manual_adjusted = true
        } else if (op.source && op.source.startsWith(oldPrefix)) {
          op.source = `${newTarget}/${op.source.slice(oldPrefix.length)}`
          op.manual_adjusted = true
        }
      }
      planEdited.value = true
      return {
        changed: true,
        opId: touchedId,
        message: `已将虚拟目录重命名为 ${newTarget}/`,
      }
    }

    // File rename
    const directOp = ops.find(
      (o) =>
        o.op_type !== 'mkdir' &&
        o.id === node.operation_id &&
        o.target === node.path,
    )

    if (directOp && directOp.source) {
      if (newTarget === directOp.source) {
        if (directOp.id.startsWith('op_manual_')) {
          const idx = ops.findIndex((o) => o.id === directOp.id)
          if (idx >= 0) ops.splice(idx, 1)
          unselectOp(directOp.id)
        } else {
          const origAiOp = originalPlanOperations.value.find((o) => o.id === directOp.id)
          if (origAiOp) {
            directOp.target = origAiOp.target
            directOp.op_type = origAiOp.op_type
          }
          directOp.manual_adjusted = false
          unselectOp(directOp.id)
        }
        planEdited.value = true
        return {
          changed: true,
          message: `已恢复原文件名 ${cleanName}，自动取消该变更`,
        }
      }

      directOp.target = newTarget
      directOp.op_type = inferOpType(directOp.source, newTarget, directOp.op_type)
      directOp.manual_adjusted = true
      ensureOpSelected(directOp.id)
      planEdited.value = true
      return {
        changed: true,
        opId: directOp.id,
        message: `已重命名为 ${cleanName}`,
      }
    }

    const uncheckedBySource = ops.find(
      (o) => o.op_type !== 'mkdir' && o.source === node.path,
    )
    if (uncheckedBySource && uncheckedBySource.source) {
      uncheckedBySource.target = newTarget
      uncheckedBySource.op_type = inferOpType(
        uncheckedBySource.source,
        newTarget,
        uncheckedBySource.op_type,
      )
      uncheckedBySource.manual_adjusted = true
      ensureOpSelected(uncheckedBySource.id)
      planEdited.value = true
      return {
        changed: true,
        opId: uncheckedBySource.id,
        message: `已重命名为 ${cleanName}`,
      }
    }

    const newOp: FileOperation = {
      id: nextManualOpId(),
      op_type: 'rename',
      source: node.path,
      target: newTarget,
      reason: '用户在预览树中双击手动重命名',
      probe_evidence: 'L1',
      category: parent === '.' ? null : parent.split('/')[0],
      size_bytes: node.size || 0,
      manual_adjusted: true,
    }
    ops.push(newOp)
    ensureOpSelected(newOp.id)
    planEdited.value = true
    return {
      changed: true,
      opId: newOp.id,
      message: `已新增重命名：${node.name} → ${cleanName}`,
    }
  }

  /**
   * Reset all manual drag/drop, mkdir, and rename edits back to the original AI plan.
   */
  function resetPlanToOriginal() {
    if (!pendingPlan.value) return
    pendingPlan.value.operations = cloneOperations(originalPlanOperations.value)
    selectedOperationIds.value = originalPlanOperations.value.map((o) => o.id)
    highlightedOperationId.value = null
    planEdited.value = false
  }

  async function confirmPendingPlan(approved: boolean, comment?: string) {
    if (!pendingPlan.value) return null

    isExecuting.value = true
    try {
      const res = await apiClient.confirmPlan({
        thread_id: pendingPlan.value.thread_id,
        plan_id: pendingPlan.value.plan_id,
        approved,
        approved_operation_ids: approved ? [...selectedOperationIds.value] : [],
        operations: approved ? cloneOperations(pendingPlan.value.operations) : null,
        user_comment: comment || null,
      })

      liveTree.value = res.updated_tree
      lastStatusMessage.value = res.message
      lastBatchResult.value = res.batch || null
      pendingPlan.value = null
      originalPlanOperations.value = []
      planEdited.value = false
      selectedOperationIds.value = []
      highlightedOperationId.value = null

      if (res.status === 'executed') {
        agentPhase.value = 'executed'
      } else if (res.status === 'cancelled') {
        agentPhase.value = 'cancelled'
      } else {
        agentPhase.value = 'error'
      }

      traceItems.value.push({
        event_id: `confirm_${Date.now()}`,
        thread_id: res.thread_id,
        timestamp: new Date().toISOString(),
        type: res.status === 'rolled_back_on_error' ? 'error' : 'execution_complete',
        probe_level: 'PLAN',
        summary: res.message,
        detail: res.batch || undefined,
      })

      // Sync history & stats
      const [cfg, hist] = await Promise.all([
        apiClient.getConfig(),
        apiClient.getHistory(),
      ])
      config.value = cfg
      historyBatches.value = hist

      return res
    } finally {
      isExecuting.value = false
    }
  }

  async function undoBatch(batchId: string) {
    isUndoingId.value = batchId
    try {
      const res = await apiClient.undoBatch(batchId)
      liveTree.value = res.updated_tree
      if (lastBatchResult.value?.batch_id === batchId) {
        lastBatchResult.value = res.batch
      }
      traceItems.value.push({
        event_id: `undo_${Date.now()}`,
        thread_id: currentThreadId.value || 'history',
        timestamp: new Date().toISOString(),
        type: 'execution_complete',
        summary: res.message,
      })
      const [cfg, hist] = await Promise.all([
        apiClient.getConfig(),
        apiClient.getHistory(),
      ])
      config.value = cfg
      historyBatches.value = hist
      return res
    } finally {
      isUndoingId.value = null
    }
  }

  function clearTrace() {
    if (isStreaming.value) return
    traceItems.value = []
    if (!pendingPlan.value) {
      agentPhase.value = 'idle'
    }
  }

  return {
    config,
    liveTree,
    historyBatches,
    isLoading,
    isStreaming,
    isExecuting,
    isSeeding,
    isPickingDirectory,
    isUndoingId,
    configDialogOpen,
    workspacePanelOpen,
    darkMode,
    activeRightTab,
    agentPhase,
    currentThreadId,
    conversationTurnCount,
    lastInstruction,
    traceItems,
    pendingPlan,
    selectedOperationIds,
    highlightedOperationId,
    lastBatchResult,
    lastStatusMessage,
    probeStats,
    selectedCount,
    totalPlanOps,
    manualAdjustedCount,
    hasManualEdits,
    undoableBatchCount,
    isLocalEndpoint,
    isModelReady,
    initialize,
    refreshWorkspace,
    saveConfig,
    selectWorkspaceViaDialog,
    switchWorkspace,
    seedDemoWorkspace,
    startAgentRun,
    abortAgentRun,
    startNewChat,
    toggleOperation,
    selectAllOperations,
    clearSelectedOperations,
    moveAfterFileToFolder,
    createVirtualFolder,
    renameAfterNode,
    resetPlanToOriginal,
    confirmPendingPlan,
    undoBatch,
    clearTrace,
    toggleDarkMode,
  }
})
