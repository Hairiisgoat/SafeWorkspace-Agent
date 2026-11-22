<script setup lang="ts">
import { computed, nextTick, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  AlertCircle,
  Archive,
  ArrowRight,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  File,
  FileArchive,
  FileCode,
  FileImage,
  FileJson,
  FileSpreadsheet,
  FileText,
  Folder,
  FolderOpen,
  FolderPlus,
  FolderTree,
  GitCompare,
  GripVertical,
  History,
  Pencil,
  RotateCcw,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  X,
} from 'lucide-vue-next'
import type {
  DiffStatus,
  ExecutionBatchRecord,
  FileOperation,
  FileTreeNode,
  OperationType,
} from '@/api/client'
import { useWorkspaceStore } from '@/stores/workspace'

const store = useWorkspaceStore()

// View state
const opFilter = ref<'all' | OperationType>('all')
const searchQuery = ref('')
const liveTreeSearch = ref('')
const collapsedBefore = reactive<Record<string, boolean>>({})
const collapsedAfter = reactive<Record<string, boolean>>({})
const collapsedLive = reactive<Record<string, boolean>>({})
const expandedBatches = reactive<Record<string, boolean>>({})

// Interactive Drag & Drop + Inline Edit state (After Tree)
const draggingNode = ref<FileTreeNode | null>(null)
const dragOverDirPath = ref<string | null>(null)
const showNewFolderBar = ref(false)
const newFolderPath = ref('')
const newFolderInputRef = ref<HTMLInputElement | null>(null)
const renamingPath = ref<string | null>(null)
const renamingDraft = ref('')
const renameInputRef = ref<HTMLInputElement[] | HTMLInputElement | null>(null)
let isFinalizingRename = false

interface FlatTreeRow {
  path: string
  depth: number
  node: FileTreeNode
  hasChildren: boolean
  expanded: boolean
}

// ---------------------------------------------------------------------------
// Live Client-Side Virtual Diff Re-computation (0ms reactivity on checkbox toggle)
// ---------------------------------------------------------------------------

function cloneCleanTree(node: FileTreeNode): FileTreeNode {
  return {
    ...node,
    diff_status: 'unchanged',
    operation_id: null,
    related_path: null,
    children: node.children.map(cloneCleanTree),
  }
}

function collectBaseMaps(
  node: FileTreeNode,
  filesMap: Map<string, FileTreeNode>,
  dirsSet: Set<string>,
) {
  if (node.type === 'dir') {
    if (node.path !== '.') dirsSet.add(node.path)
    for (const c of node.children) collectBaseMaps(c, filesMap, dirsSet)
  } else {
    filesMap.set(node.path, { ...node, diff_status: 'unchanged', operation_id: null, related_path: null, children: [] })
  }
}

function buildSimulatedTrees(
  rawBefore: FileTreeNode,
  allOps: FileOperation[],
  selectedIds: Set<string>,
): { before: FileTreeNode; after: FileTreeNode } {
  const baseBefore = cloneCleanTree(rawBefore)
  const filesMap = new Map<string, FileTreeNode>()
  const baseDirs = new Set<string>()
  collectBaseMaps(baseBefore, filesMap, baseDirs)

  const virtualDirs = new Map<
    string,
    { diff_status: DiffStatus; operation_id: string | null; related_path: string | null }
  >()
  for (const d of baseDirs) {
    virtualDirs.set(d, { diff_status: 'unchanged', operation_id: null, related_path: null })
  }

  const sourceMarks = new Map<string, { status: DiffStatus; opId: string; target: string }>()
  const activeOps = allOps
    .filter((o) => selectedIds.has(o.id))
    .sort((a, b) => (a.op_type === 'mkdir' ? 0 : 1) - (b.op_type === 'mkdir' ? 0 : 1))

  for (const op of activeOps) {
    const targetParts = op.target.split('/').filter(Boolean)
    if (op.op_type === 'mkdir') {
      for (let i = 1; i <= targetParts.length; i++) {
        const sub = targetParts.slice(0, i).join('/')
        if (!virtualDirs.has(sub)) {
          virtualDirs.set(sub, { diff_status: 'added', operation_id: op.id, related_path: null })
        }
      }
      continue
    }

    if (!op.source) continue
    const beforeStatus: DiffStatus = op.op_type === 'rename' ? 'renamed_from' : 'moved_from'
    const afterStatus: DiffStatus =
      op.op_type === 'rename'
        ? 'renamed_to'
        : op.op_type === 'archive'
          ? 'archived_to'
          : 'moved_to'

    sourceMarks.set(op.source, { status: beforeStatus, opId: op.id, target: op.target })

    // Ensure parent directories exist in virtual after tree
    for (let i = 1; i < targetParts.length; i++) {
      const parent = targetParts.slice(0, i).join('/')
      if (!virtualDirs.has(parent)) {
        virtualDirs.set(parent, { diff_status: 'added', operation_id: op.id, related_path: null })
      }
    }

    if (filesMap.has(op.source)) {
      const fileMeta = filesMap.get(op.source)!
      filesMap.delete(op.source)
      const targetName = targetParts[targetParts.length - 1] || op.target
      filesMap.set(op.target, {
        ...fileMeta,
        name: targetName,
        path: op.target,
        diff_status: afterStatus,
        operation_id: op.id,
        related_path: op.source,
      })
    } else if (virtualDirs.has(op.source)) {
      virtualDirs.delete(op.source)
      virtualDirs.set(op.target, {
        diff_status: afterStatus,
        operation_id: op.id,
        related_path: op.source,
      })
      const prefix = `${op.source}/`
      for (const [fPath, fMeta] of Array.from(filesMap.entries())) {
        if (fPath.startsWith(prefix)) {
          filesMap.delete(fPath)
          const nextPath = `${op.target}/${fPath.slice(prefix.length)}`
          filesMap.set(nextPath, {
            ...fMeta,
            path: nextPath,
            diff_status: afterStatus,
            operation_id: op.id,
            related_path: fPath,
          })
        }
      }
    }
  }

  // Apply before annotations
  const applyBefore = (n: FileTreeNode) => {
    const mark = sourceMarks.get(n.path)
    if (mark) {
      n.diff_status = mark.status
      n.operation_id = mark.opId
      n.related_path = mark.target
    }
    for (const c of n.children) applyBefore(c)
  }
  applyBefore(baseBefore)

  // Reconstruct sorted after tree
  const afterRoot: FileTreeNode = {
    name: rawBefore.name,
    path: '.',
    type: 'dir',
    size: 0,
    diff_status: 'unchanged',
    children: [],
  }
  const dirNodes = new Map<string, FileTreeNode>([['.', afterRoot]])

  const ensureDirNode = (rel: string): FileTreeNode => {
    const existing = dirNodes.get(rel)
    if (existing) return existing
    const parts = rel.split('/').filter(Boolean)
    const parentRel = parts.length === 1 ? '.' : parts.slice(0, -1).join('/')
    const parent = ensureDirNode(parentRel)
    const meta = virtualDirs.get(rel)
    const created: FileTreeNode = {
      name: parts[parts.length - 1],
      path: rel,
      type: 'dir',
      size: 0,
      diff_status: meta?.diff_status || 'unchanged',
      operation_id: meta?.operation_id || null,
      related_path: meta?.related_path || null,
      children: [],
    }
    dirNodes.set(rel, created)
    parent.children.push(created)
    return created
  }

  const sortedDirKeys = Array.from(virtualDirs.keys()).sort((a, b) => {
    const da = a.split('/').length
    const db = b.split('/').length
    return da !== db ? da - db : a.localeCompare(b)
  })
  for (const d of sortedDirKeys) ensureDirNode(d)

  for (const [fRel, fNode] of filesMap.entries()) {
    const parts = fRel.split('/').filter(Boolean)
    const parentRel = parts.length === 1 ? '.' : parts.slice(0, -1).join('/')
    const parent = ensureDirNode(parentRel)
    parent.children.push(fNode)
  }

  const sortNode = (n: FileTreeNode) => {
    n.children.sort((a, b) => {
      if (a.type !== b.type) return a.type === 'dir' ? -1 : 1
      return a.name.toLowerCase().localeCompare(b.name.toLowerCase())
    })
    for (const c of n.children) sortNode(c)
  }
  sortNode(afterRoot)

  return { before: baseBefore, after: afterRoot }
}

const simulatedDiff = computed(() => {
  if (!store.pendingPlan) return null
  return buildSimulatedTrees(
    store.pendingPlan.before_tree,
    store.pendingPlan.operations,
    new Set(store.selectedOperationIds),
  )
})

function flattenTree(
  root: FileTreeNode | null,
  collapsedMap: Record<string, boolean>,
  filterKeyword = '',
): FlatTreeRow[] {
  if (!root) return []
  const kw = filterKeyword.trim().toLowerCase()
  const rows: FlatTreeRow[] = []

  const matchesFilter = (n: FileTreeNode): boolean => {
    if (!kw) return true
    if (n.name.toLowerCase().includes(kw) || n.path.toLowerCase().includes(kw)) return true
    return n.children.some(matchesFilter)
  }

  const visit = (n: FileTreeNode, depth: number) => {
    if (n.path !== '.') {
      if (!matchesFilter(n)) return
      const hasChildren = n.type === 'dir' && n.children.length > 0
      const expanded = !collapsedMap[n.path]
      rows.push({ path: n.path, depth, node: n, hasChildren, expanded })
      if (n.type === 'dir' && !expanded) return
    }
    for (const child of n.children) {
      visit(child, n.path === '.' ? 0 : depth + 1)
    }
  }

  visit(root, 0)
  return rows
}

const beforeRows = computed(() =>
  flattenTree(simulatedDiff.value?.before || null, collapsedBefore),
)
const afterRows = computed(() =>
  flattenTree(simulatedDiff.value?.after || null, collapsedAfter),
)
const liveRows = computed(() =>
  flattenTree(store.liveTree, collapsedLive, liveTreeSearch.value),
)

const afterExistingPaths = computed(() => {
  const set = new Set<string>()
  const root = simulatedDiff.value?.after
  if (!root) return set
  const walk = (n: FileTreeNode) => {
    if (n.path !== '.') set.add(n.path.toLowerCase())
    for (const c of n.children) walk(c)
  }
  walk(root)
  return set
})

const manualOpIdSet = computed(() => {
  const set = new Set<string>()
  for (const op of store.pendingPlan?.operations || []) {
    if (op.manual_adjusted) set.add(op.id)
  }
  return set
})

function isNodeManuallyAdjusted(node: FileTreeNode): boolean {
  return Boolean(node.operation_id && manualOpIdSet.value.has(node.operation_id))
}

function canRenameAfterNode(node: FileTreeNode): boolean {
  return node.type === 'file' || (node.type === 'dir' && node.diff_status === 'added')
}

// Filtered operation checklist
const filteredOperations = computed(() => {
  if (!store.pendingPlan) return []
  const kw = searchQuery.value.trim().toLowerCase()
  return store.pendingPlan.operations.filter((op) => {
    if (opFilter.value !== 'all' && op.op_type !== opFilter.value) return false
    if (!kw) return true
    return (
      op.target.toLowerCase().includes(kw) ||
      (op.source || '').toLowerCase().includes(kw) ||
      op.reason.toLowerCase().includes(kw)
    )
  })
})

const selectedStats = computed(() => {
  const ops = (store.pendingPlan?.operations || []).filter((o) =>
    store.selectedOperationIds.includes(o.id),
  )
  return {
    mkdir: ops.filter((o) => o.op_type === 'mkdir').length,
    move: ops.filter((o) => o.op_type === 'move').length,
    rename: ops.filter((o) => o.op_type === 'rename').length,
    archive: ops.filter((o) => o.op_type === 'archive').length,
  }
})

watch(
  () => store.pendingPlan?.plan_id,
  () => {
    opFilter.value = 'all'
    searchQuery.value = ''
    draggingNode.value = null
    dragOverDirPath.value = null
    showNewFolderBar.value = false
    newFolderPath.value = ''
    renamingPath.value = null
    renamingDraft.value = ''
    Object.keys(collapsedBefore).forEach((k) => delete collapsedBefore[k])
    Object.keys(collapsedAfter).forEach((k) => delete collapsedAfter[k])
  },
)

// ---------------------------------------------------------------------------
// HTML5 Drag & Drop Handlers (After Tree)
// ---------------------------------------------------------------------------

function onDragStart(e: DragEvent, node: FileTreeNode) {
  if (node.type !== 'file' || renamingPath.value === node.path) {
    e.preventDefault()
    return
  }
  draggingNode.value = node
  dragOverDirPath.value = null
  if (e.dataTransfer) {
    e.dataTransfer.effectAllowed = 'move'
    e.dataTransfer.setData('text/plain', node.path)
  }
}

function onDragEnd() {
  draggingNode.value = null
  dragOverDirPath.value = null
}

function onDragOverFolder(e: DragEvent, targetDirPath: string) {
  if (!draggingNode.value) return
  e.preventDefault()
  if (e.dataTransfer) {
    e.dataTransfer.dropEffect = 'move'
  }
  dragOverDirPath.value = targetDirPath
}

function onDragLeaveFolder(e: DragEvent, targetDirPath: string) {
  const current = e.currentTarget as HTMLElement | null
  const related = e.relatedTarget as Node | null
  if (current && related && current.contains(related)) return
  if (dragOverDirPath.value === targetDirPath) {
    dragOverDirPath.value = null
  }
}

function onDropOnFolder(e: DragEvent, targetDirPath: string) {
  e.preventDefault()
  const dragged = draggingNode.value
  draggingNode.value = null
  dragOverDirPath.value = null
  if (!dragged) return

  try {
    const res = store.moveAfterFileToFolder(
      dragged,
      targetDirPath,
      afterExistingPaths.value,
    )
    if (res.changed) {
      if (targetDirPath !== '.') {
        collapsedAfter[targetDirPath] = false
      }
      if (res.opId) {
        store.highlightedOperationId = res.opId
      }
      ElMessage.success(res.message)
    }
  } catch (err) {
    ElMessage.warning(err instanceof Error ? err.message : '拖拽归类失败')
  }
}

// ---------------------------------------------------------------------------
// Virtual Folder Creation & Inline Double-Click Rename
// ---------------------------------------------------------------------------

async function openNewFolderBar() {
  showNewFolderBar.value = true
  newFolderPath.value = ''
  await nextTick()
  newFolderInputRef.value?.focus()
}

function cancelNewFolder() {
  showNewFolderBar.value = false
  newFolderPath.value = ''
}

function submitNewFolder() {
  const raw = newFolderPath.value.trim()
  if (!raw) {
    cancelNewFolder()
    return
  }
  try {
    const created = store.createVirtualFolder(raw, afterExistingPaths.value)
    // Expand parent folders so newly created directory is immediately visible
    const parts = created.target.split('/')
    for (let i = 1; i <= parts.length; i++) {
      collapsedAfter[parts.slice(0, i).join('/')] = false
    }
    store.highlightedOperationId = created.id
    showNewFolderBar.value = false
    newFolderPath.value = ''
    ElMessage.success(`已新建虚拟目录：${created.target}/，可直接将文件拖入`)
  } catch (err) {
    ElMessage.warning(err instanceof Error ? err.message : '创建目录失败')
  }
}

async function startInlineRename(node: FileTreeNode) {
  if (!canRenameAfterNode(node)) return
  renamingPath.value = node.path
  renamingDraft.value = node.name
  isFinalizingRename = false
  await nextTick()
  const inputEl = Array.isArray(renameInputRef.value)
    ? renameInputRef.value[0]
    : renameInputRef.value
  if (inputEl) {
    inputEl.focus()
    if (node.type === 'file') {
      const dotIdx = node.name.lastIndexOf('.')
      if (dotIdx > 0) {
        inputEl.setSelectionRange(0, dotIdx)
      } else {
        inputEl.select()
      }
    } else {
      inputEl.select()
    }
  }
}

function cancelInlineRename() {
  isFinalizingRename = true
  renamingPath.value = null
  renamingDraft.value = ''
}

function commitInlineRename(node: FileTreeNode) {
  if (isFinalizingRename || renamingPath.value !== node.path) return
  isFinalizingRename = true
  const draft = renamingDraft.value.trim()
  renamingPath.value = null
  renamingDraft.value = ''

  if (!draft || draft === node.name) return
  try {
    const res = store.renameAfterNode(node, draft, afterExistingPaths.value)
    if (res.changed) {
      if (res.opId) store.highlightedOperationId = res.opId
      ElMessage.success(res.message)
    }
  } catch (err) {
    ElMessage.warning(err instanceof Error ? err.message : '重命名失败')
  }
}

function handleResetPlan() {
  store.resetPlanToOriginal()
  showNewFolderBar.value = false
  renamingPath.value = null
  ElMessage.info('已重置为 AI 原始整理方案')
}

// Helpers
function toggleFolder(map: Record<string, boolean>, path: string) {
  map[path] = !map[path]
}

function getFileIcon(node: FileTreeNode, expanded = false) {
  if (node.type === 'dir') {
    if (node.diff_status === 'added') return FolderPlus
    return expanded ? FolderOpen : Folder
  }
  const ext = (node.extension || '').toLowerCase()
  if (['.py', '.ts', '.tsx', '.js', '.vue', '.go', '.rs', '.java', '.sh', '.sql', '.html', '.css'].includes(ext)) {
    return FileCode
  }
  if (['.json', '.jsonl', '.yaml', '.yml', '.toml'].includes(ext)) return FileJson
  if (['.csv', '.tsv', '.xlsx', '.xls'].includes(ext)) return FileSpreadsheet
  if (['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'].includes(ext)) return FileImage
  if (['.zip', '.tar', '.gz', '.7z', '.rar'].includes(ext)) return FileArchive
  if (['.md', '.txt', '.pdf', '.doc', '.docx', '.log'].includes(ext)) return FileText
  return File
}

function formatSize(bytes: number): string {
  if (!bytes) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function formatDateTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

function opTypeLabel(opType: OperationType): string {
  const map: Record<OperationType, string> = {
    mkdir: '新建目录',
    move: '移动',
    rename: '重命名',
    archive: '归档',
  }
  return map[opType]
}

function diffBadgeText(status: DiffStatus): string {
  const map: Record<DiffStatus, string> = {
    unchanged: '',
    added: '+ 新增',
    removed: '- 移除',
    moved_from: '↗ 移出',
    moved_to: '→ 移入',
    renamed_from: '✎ 原名',
    renamed_to: '✎ 新名',
    archived_to: '归档',
  }
  return map[status]
}

function batchStatusLabel(status: ExecutionBatchRecord['status']): string {
  const map: Record<ExecutionBatchRecord['status'], string> = {
    executed: '已执行 · 可撤回',
    reverted: '已撤回',
    rolled_back_on_error: '异常已自动回滚',
    cancelled: '已取消',
  }
  return map[status]
}

// Actions
async function handleExecute() {
  if (store.selectedCount === 0) {
    ElMessage.warning('请至少勾选一项要执行的变更')
    return
  }
  try {
    const res = await store.confirmPendingPlan(true)
    if (res?.status === 'executed') {
      ElMessage.success('事务级整理已完成，回滚日志已写入 .agent_history/')
    } else if (res?.status === 'rolled_back_on_error') {
      ElMessage.error(res.message)
    }
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '执行失败')
  }
}

async function handleReject() {
  try {
    await store.confirmPendingPlan(false)
    ElMessage.info('已拒绝本次提案，未对本地沙盒产生任何修改')
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '取消失败')
  }
}

async function handleUndo(batchId: string) {
  try {
    await ElMessageBox.confirm(
      `确定将批次 ${batchId} 完整回滚至整理前状态吗？事务执行器将逆序还原所有移动/重命名文件，并安全清理新建的空目录。`,
      '确认一键撤回 (Transactional Undo)',
      {
        confirmButtonText: '确认撤回',
        cancelButtonText: '取消',
        type: 'warning',
      },
    )
    await store.undoBatch(batchId)
    ElMessage.success(`批次 ${batchId} 已完整撤回`)
  } catch (err) {
    if (err === 'cancel' || err === 'close') return
    ElMessage.error(err instanceof Error ? err.message : '撤回失败')
  }
}

async function handleSeedDemo() {
  try {
    const res = await store.seedDemoWorkspace()
    if (res.created_files.length > 0) {
      ElMessage.success(`已生成 ${res.created_files.length} 个示例杂乱文件，现在可以向左侧智能体下达整理指令！`)
    } else {
      ElMessage.info('示例文件已存在')
    }
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '生成示例文件失败')
  }
}
</script>

<template>
  <main class="diff-workspace">
    <!-- Top Segmented Tabs -->
    <div class="workspace-tabs-bar">
      <div class="segmented-tabs">
        <button
          type="button"
          class="seg-tab"
          :class="{ active: store.activeRightTab === 'diff' }"
          @click="store.activeRightTab = 'diff'"
        >
          <GitCompare :size="14" />
          <span>{{ store.pendingPlan ? '目录 Diff 对比与人工审批' : '沙盒实时目录树' }}</span>
          <span v-if="store.pendingPlan" class="tab-pill interrupt mono">
            {{ store.selectedCount }}/{{ store.totalPlanOps }}
          </span>
        </button>

        <button
          type="button"
          class="seg-tab"
          :class="{ active: store.activeRightTab === 'history' }"
          @click="store.activeRightTab = 'history'"
        >
          <History :size="14" />
          <span>事务回滚日志 (.agent_history)</span>
          <span v-if="store.historyBatches.length > 0" class="tab-pill mono">
            {{ store.historyBatches.length }}
          </span>
        </button>
      </div>

      <div v-if="store.pendingPlan && store.activeRightTab === 'diff'" class="stats-strip mono">
        <span class="stat-pill add">+{{ selectedStats.mkdir }} 目录</span>
        <span class="stat-pill move">→{{ selectedStats.move }} 移动</span>
        <span class="stat-pill rename">✎{{ selectedStats.rename }} 重命名</span>
        <span v-if="selectedStats.archive > 0" class="stat-pill archive">
          归档 {{ selectedStats.archive }}
        </span>
      </div>
    </div>

    <!-- =================================================================== -->
    <!-- TAB 1: DIFF PREVIEW & HUMAN APPROVAL (OR LIVE TREE WHEN IDLE)       -->
    <!-- =================================================================== -->
    <div v-if="store.activeRightTab === 'diff'" class="tab-body">
      <!-- ACTIVE SUSPENDED PLAN: SIDE-BY-SIDE DIFF + INTERACTIVE CHECKLIST -->
      <template v-if="store.pendingPlan && simulatedDiff">
        <div class="plan-scroll-area">
          <!-- Plan Summary Banner -->
          <div class="plan-summary-card">
            <div class="plan-summary-head">
              <div class="plan-title-group">
                <span class="hitl-badge">Human-in-the-Loop · 待审批</span>
                <span class="plan-id mono">{{ store.pendingPlan.plan_id }}</span>
                <span v-if="store.hasManualEdits" class="manual-badge">
                  <SlidersHorizontal :size="10" />
                  <span>已手动微调 ({{ store.manualAdjustedCount }})</span>
                </span>
              </div>
              <div class="plan-head-right">
                <button
                  v-if="store.hasManualEdits"
                  type="button"
                  class="btn-reset-plan"
                  title="撤销所有拖拽、新建文件夹和重命名微调，恢复大模型原始方案"
                  @click="handleResetPlan"
                >
                  <RotateCcw :size="12" />
                  <span>重置为 AI 原始方案</span>
                </button>
                <span class="zero-write-note">
                  <ShieldCheck :size="13" />
                  尚未写入磁盘 · 支持拖拽归类与双击重命名
                </span>
              </div>
            </div>
            <p class="plan-summary-text">{{ store.pendingPlan.summary }}</p>

            <div v-if="store.pendingPlan.warnings.length > 0" class="plan-warnings">
              <div v-for="(w, i) in store.pendingPlan.warnings" :key="i" class="warning-line">
                <AlertCircle :size="13" />
                <span>{{ w }}</span>
              </div>
            </div>
          </div>

          <!-- Side-by-Side Directory Tree Comparison -->
          <section class="split-trees-grid">
            <!-- BEFORE TREE -->
            <div class="tree-card">
              <div class="tree-card-header">
                <span class="tree-card-title">整理前 (Before)</span>
                <span class="tree-card-sub mono">{{ simulatedDiff.before.name }}/</span>
              </div>
              <div class="tree-list">
                <div
                  v-for="row in beforeRows"
                  :key="`b_${row.path}`"
                  class="tree-row"
                  :class="[
                    `diff-${row.node.diff_status}`,
                    {
                      highlighted:
                        row.node.operation_id &&
                        row.node.operation_id === store.highlightedOperationId,
                    },
                  ]"
                  :style="{ paddingLeft: `${10 + row.depth * 18}px` }"
                  @mouseenter="store.highlightedOperationId = row.node.operation_id || null"
                  @mouseleave="store.highlightedOperationId = null"
                  @click="
                    row.node.type === 'dir'
                      ? toggleFolder(collapsedBefore, row.path)
                      : null
                  "
                >
                  <span class="chevron-slot">
                    <template v-if="row.hasChildren">
                      <ChevronDown v-if="row.expanded" :size="12" />
                      <ChevronRight v-else :size="12" />
                    </template>
                  </span>
                  <component
                    :is="getFileIcon(row.node, row.expanded)"
                    :size="14"
                    class="node-icon"
                    :class="{ folder: row.node.type === 'dir' }"
                  />
                  <span class="node-name mono" :title="row.path">{{ row.node.name }}</span>
                  <span
                    v-if="row.node.diff_status !== 'unchanged'"
                    class="diff-tag"
                    :class="row.node.diff_status"
                  >
                    {{ diffBadgeText(row.node.diff_status) }}
                  </span>
                  <span
                    v-if="row.node.related_path"
                    class="related-hint mono"
                    :title="`目标路径: ${row.node.related_path}`"
                  >
                    → {{ row.node.related_path }}
                  </span>
                </div>
              </div>
            </div>

            <!-- AFTER TREE (DYNAMICALLY SIMULATED + DRAG & DROP + INLINE EDIT) -->
            <div class="tree-card after-card">
              <div class="tree-card-header after-tree-header">
                <div class="after-title-group">
                  <span class="tree-card-title">整理后预览 (After · 可拖拽微调)</span>
                </div>
                <div class="tree-toolbar-actions">
                  <button
                    type="button"
                    class="tree-mini-btn primary"
                    title="在预览树中新建目标虚拟目录，方便拖入文件"
                    @click="openNewFolderBar"
                  >
                    <FolderPlus :size="12" />
                    <span>+ 新建文件夹</span>
                  </button>
                  <button
                    v-if="store.hasManualEdits"
                    type="button"
                    class="tree-mini-btn"
                    title="重置为 AI 原始方案"
                    @click="handleResetPlan"
                  >
                    <RotateCcw :size="11" />
                    <span>重置</span>
                  </button>
                </div>
              </div>

              <!-- Inline Create Virtual Folder Bar -->
              <div v-if="showNewFolderBar" class="new-folder-bar">
                <FolderPlus :size="14" class="node-icon folder" />
                <input
                  ref="newFolderInputRef"
                  v-model="newFolderPath"
                  type="text"
                  class="new-folder-input mono"
                  placeholder="输入目录名或相对路径，如 参考资料 或 文档/作业..."
                  @keyup.enter="submitNewFolder"
                  @keyup.esc="cancelNewFolder"
                />
                <button type="button" class="mini-confirm-btn" @click="submitNewFolder">
                  创建
                </button>
                <button type="button" class="mini-cancel-btn" @click="cancelNewFolder">
                  取消
                </button>
              </div>

              <!-- Root Dropzone Strip -->
              <div
                class="root-drop-row"
                :class="{
                  'drag-active': Boolean(draggingNode),
                  'dropzone-active': dragOverDirPath === '.',
                }"
                @dragover="onDragOverFolder($event, '.')"
                @dragleave="onDragLeaveFolder($event, '.')"
                @drop="onDropOnFolder($event, '.')"
              >
                <FolderOpen :size="13" class="node-icon folder" />
                <span class="mono root-drop-name">{{ simulatedDiff.after.name }}/</span>
                <span v-if="dragOverDirPath === '.'" class="drop-pill">
                  松开移回根目录
                </span>
                <span v-else-if="draggingNode" class="root-drop-hint">
                  拖放到此处移回根目录
                </span>
                <span v-else class="root-drop-hint">
                  拖拽文件到文件夹 · 双击改名
                </span>
              </div>

              <div class="tree-list">
                <div
                  v-for="row in afterRows"
                  :key="`a_${row.path}`"
                  class="tree-row after-row"
                  :class="[
                    `diff-${row.node.diff_status}`,
                    {
                      draggable: row.node.type === 'file' && renamingPath !== row.path,
                      'is-dragging': draggingNode?.path === row.path,
                      'dropzone-active': row.node.type === 'dir' && dragOverDirPath === row.path,
                      highlighted:
                        row.node.operation_id &&
                        row.node.operation_id === store.highlightedOperationId,
                    },
                  ]"
                  :style="{ paddingLeft: `${8 + row.depth * 18}px` }"
                  :draggable="row.node.type === 'file' && renamingPath !== row.path"
                  @dragstart="onDragStart($event, row.node)"
                  @dragend="onDragEnd"
                  @dragover="row.node.type === 'dir' ? onDragOverFolder($event, row.path) : undefined"
                  @dragleave="row.node.type === 'dir' ? onDragLeaveFolder($event, row.path) : undefined"
                  @drop="row.node.type === 'dir' ? onDropOnFolder($event, row.path) : undefined"
                  @mouseenter="store.highlightedOperationId = row.node.operation_id || null"
                  @mouseleave="store.highlightedOperationId = null"
                  @click="
                    row.node.type === 'dir' && renamingPath !== row.path
                      ? toggleFolder(collapsedAfter, row.path)
                      : null
                  "
                  @dblclick.stop="startInlineRename(row.node)"
                >
                  <span class="chevron-slot">
                    <template v-if="row.hasChildren">
                      <ChevronDown v-if="row.expanded" :size="12" />
                      <ChevronRight v-else :size="12" />
                    </template>
                    <GripVertical
                      v-else-if="row.node.type === 'file'"
                      :size="11"
                      class="drag-grip"
                    />
                  </span>
                  <component
                    :is="getFileIcon(row.node, row.expanded)"
                    :size="14"
                    class="node-icon"
                    :class="{ folder: row.node.type === 'dir' }"
                  />

                  <!-- Inline Rename Input OR Node Name -->
                  <input
                    v-if="renamingPath === row.path"
                    ref="renameInputRef"
                    v-model="renamingDraft"
                    type="text"
                    class="inline-rename-input mono"
                    @click.stop
                    @dblclick.stop
                    @keydown.enter.stop.prevent="commitInlineRename(row.node)"
                    @keydown.esc.stop.prevent="cancelInlineRename"
                    @blur="commitInlineRename(row.node)"
                  />
                  <span
                    v-else
                    class="node-name mono"
                    :title="
                      canRenameAfterNode(row.node)
                        ? `${row.path}（双击重命名${row.node.type === 'file' ? '，按住拖拽调整目录' : ''}）`
                        : row.path
                    "
                  >
                    {{ row.node.name }}
                  </span>

                  <!-- Dropzone Pill -->
                  <span
                    v-if="row.node.type === 'dir' && dragOverDirPath === row.path"
                    class="drop-pill"
                  >
                    松开移入此目录
                  </span>

                  <template v-else-if="renamingPath !== row.path">
                    <span
                      v-if="row.node.diff_status !== 'unchanged'"
                      class="diff-tag"
                      :class="row.node.diff_status"
                    >
                      {{ diffBadgeText(row.node.diff_status) }}
                    </span>

                    <span v-if="isNodeManuallyAdjusted(row.node)" class="manual-badge compact">
                      手动调整
                    </span>

                    <button
                      v-if="canRenameAfterNode(row.node)"
                      type="button"
                      class="row-rename-trigger"
                      title="重命名 (或直接双击名称)"
                      @click.stop="startInlineRename(row.node)"
                    >
                      <Pencil :size="11" />
                    </button>

                    <span
                      v-if="row.node.diff_status === 'unchanged' && row.node.size"
                      class="size-hint mono"
                    >
                      {{ formatSize(row.node.size) }}
                    </span>
                  </template>
                </div>
              </div>
            </div>
          </section>

          <!-- Granular Operation Approval Checklist -->
          <section class="ops-section">
            <div class="ops-toolbar">
              <div class="ops-title-row">
                <span class="ops-heading">原子变更清单（支持逐条勾选审批）</span>
                <div class="filter-pills">
                  <button
                    v-for="f in (['all', 'mkdir', 'move', 'rename', 'archive'] as const)"
                    :key="f"
                    type="button"
                    class="filter-pill"
                    :class="{ active: opFilter === f }"
                    @click="opFilter = f"
                  >
                    {{ f === 'all' ? '全部' : opTypeLabel(f) }}
                  </button>
                </div>
              </div>

              <div class="ops-search-box">
                <Search :size="13" class="search-icon" />
                <input
                  v-model="searchQuery"
                  type="text"
                  class="mini-search"
                  placeholder="筛选路径或分类原因..."
                />
              </div>
            </div>

            <div class="ops-list">
              <div
                v-for="op in filteredOperations"
                :key="op.id"
                class="op-item"
                :class="{
                  unchecked: !store.selectedOperationIds.includes(op.id),
                  highlighted: store.highlightedOperationId === op.id,
                }"
                @mouseenter="store.highlightedOperationId = op.id"
                @mouseleave="store.highlightedOperationId = null"
              >
                <div class="op-check" @click.stop>
                  <el-checkbox
                    :model-value="store.selectedOperationIds.includes(op.id)"
                    @change="store.toggleOperation(op.id)"
                  />
                </div>

                <div class="op-main" @click="store.toggleOperation(op.id)">
                  <div class="op-top-line">
                    <span class="op-type-badge" :class="op.op_type">
                      <FolderPlus v-if="op.op_type === 'mkdir'" :size="11" />
                      <ArrowRight v-else-if="op.op_type === 'move'" :size="11" />
                      <Pencil v-else-if="op.op_type === 'rename'" :size="11" />
                      <Archive v-else :size="11" />
                      <span>{{ opTypeLabel(op.op_type) }}</span>
                    </span>

                    <span class="evidence-badge mono" :class="op.probe_evidence.toLowerCase()">
                      {{ op.probe_evidence }}
                    </span>

                    <span v-if="op.manual_adjusted" class="manual-badge">
                      <SlidersHorizontal :size="10" />
                      <span>手动调整</span>
                    </span>

                    <div class="op-paths mono">
                      <template v-if="op.source">
                        <span class="path-src">{{ op.source }}</span>
                        <ArrowRight :size="12" class="arrow-sep" />
                        <span class="path-dst">{{ op.target }}</span>
                      </template>
                      <template v-else>
                        <span class="path-dst">{{ op.target }}/</span>
                      </template>
                    </div>

                    <span v-if="op.size_bytes" class="op-size mono">
                      {{ formatSize(op.size_bytes) }}
                    </span>
                  </div>

                  <div class="op-reason">{{ op.reason }}</div>
                </div>
              </div>
            </div>
          </section>
        </div>

        <!-- Sticky Human Approval Footer -->
        <footer class="approval-footer">
          <div class="footer-left">
            <span class="selection-summary">
              已勾选 <strong class="mono">{{ store.selectedCount }}</strong> /
              <span class="mono">{{ store.totalPlanOps }}</span> 项变更
            </span>
            <button type="button" class="link-btn" @click="store.selectAllOperations()">
              全选
            </button>
            <button type="button" class="link-btn" @click="store.clearSelectedOperations()">
              全不选
            </button>
          </div>

          <div class="footer-actions">
            <button
              type="button"
              class="btn-reject"
              :disabled="store.isExecuting"
              @click="handleReject"
            >
              <X :size="14" />
              <span>拒绝并取消</span>
            </button>

            <button
              type="button"
              class="btn-approve"
              :disabled="store.isExecuting || store.selectedCount === 0"
              @click="handleExecute"
            >
              <Check :size="14" />
              <span>
                {{
                  store.isExecuting
                    ? '事务执行中...'
                    : `确认执行所选 (${store.selectedCount})`
                }}
              </span>
            </button>
          </div>
        </footer>
      </template>

      <!-- IDLE / POST-EXECUTION LIVE SANDBOX DIRECTORY TREE -->
      <template v-else>
        <div class="live-scroll-area">
          <!-- Post-execution Success Banner with Instant One-Click Undo -->
          <div
            v-if="store.lastBatchResult && store.lastBatchResult.status === 'executed'"
            class="post-exec-banner"
          >
            <div class="banner-left">
              <CheckCircle2 :size="16" class="banner-icon" />
              <div>
                <div class="banner-title">
                  事务执行完成 · 批次 <code class="mono">{{ store.lastBatchResult.batch_id }}</code>
                </div>
                <div class="banner-sub">
                  已安全执行 {{ store.lastBatchResult.approved_count }} 项变更
                  <template v-if="store.lastBatchResult.skipped_count > 0">
                    （跳过 {{ store.lastBatchResult.skipped_count }} 项未勾选）
                  </template>
                  ，完整回滚日志已持久化至 <code>.agent_history/</code>。
                </div>
              </div>
            </div>

            <button
              type="button"
              class="btn-undo-banner"
              :disabled="store.isUndoingId === store.lastBatchResult.batch_id"
              @click="handleUndo(store.lastBatchResult.batch_id)"
            >
              <RotateCcw :size="13" />
              <span>
                {{
                  store.isUndoingId === store.lastBatchResult.batch_id
                    ? '撤回中...'
                    : '一键撤回 (Undo)'
                }}
              </span>
            </button>
          </div>

          <!-- Live Workspace Tree Card -->
          <div class="live-tree-card">
            <div class="live-tree-head">
              <div class="live-title-group">
                <FolderTree :size="15" class="icon-muted" />
                <span class="live-title">当前沙盒目录快照</span>
                <span v-if="store.liveTree" class="live-root-tag mono">
                  {{ store.liveTree.name }}/
                </span>
              </div>

              <div class="ops-search-box">
                <Search :size="13" class="search-icon" />
                <input
                  v-model="liveTreeSearch"
                  type="text"
                  class="mini-search"
                  placeholder="快速检索沙盒文件..."
                />
              </div>
            </div>

            <!-- Empty Sandbox Onboarding -->
            <div v-if="liveRows.length === 0 && !liveTreeSearch" class="empty-sandbox">
              <Folder :size="26" class="icon-muted" />
              <h4>当前沙盒工作区为空</h4>
              <p>
                您可以在本地目录 <code>{{ store.config?.sandbox_root }}</code> 中放入待整理文件，
                或一键生成包含发票、会议纪要、代码与旧归档的测试杂乱文件集。
              </p>
              <button
                type="button"
                class="btn-approve"
                :disabled="store.isSeeding"
                @click="handleSeedDemo"
              >
                <Sparkles :size="14" />
                <span>{{ store.isSeeding ? '生成中...' : '生成测试杂乱文件' }}</span>
              </button>
            </div>

            <!-- File Rows -->
            <div v-else class="live-tree-list">
              <div
                v-for="row in liveRows"
                :key="`live_${row.path}`"
                class="tree-row live"
                :style="{ paddingLeft: `${14 + row.depth * 20}px` }"
                @click="
                  row.node.type === 'dir' ? toggleFolder(collapsedLive, row.path) : null
                "
              >
                <span class="chevron-slot">
                  <template v-if="row.hasChildren">
                    <ChevronDown v-if="row.expanded" :size="13" />
                    <ChevronRight v-else :size="13" />
                  </template>
                </span>
                <component
                  :is="getFileIcon(row.node, row.expanded)"
                  :size="15"
                  class="node-icon"
                  :class="{ folder: row.node.type === 'dir' }"
                />
                <span class="node-name mono">{{ row.node.name }}</span>
                <span v-if="row.node.extension" class="ext-pill mono">
                  {{ row.node.extension }}
                </span>
                <span class="live-row-meta mono">
                  <template v-if="row.node.type === 'file'">
                    {{ formatSize(row.node.size) }}
                  </template>
                  <template v-else>
                    {{ row.node.children.length }} 项
                  </template>
                </span>
              </div>
            </div>
          </div>
        </div>
      </template>
    </div>

    <!-- =================================================================== -->
    <!-- TAB 2: TRANSACTIONAL HISTORY & ONE-CLICK UNDO (.agent_history/)     -->
    <!-- =================================================================== -->
    <div v-else class="tab-body history-scroll-area">
      <div v-if="store.historyBatches.length === 0" class="empty-sandbox">
        <History :size="26" class="icon-muted" />
        <h4>暂无事务执行记录</h4>
        <p>
          每次经由您人工确认执行的目录整理批次，均会生成不可篡改的事务日志保存在
          <code>.agent_history/</code> 中，支持随时原子级一键撤回。
        </p>
      </div>

      <div v-else class="history-list">
        <article
          v-for="batch in store.historyBatches"
          :key="batch.batch_id"
          class="history-card"
        >
          <div class="history-card-head">
            <div class="history-head-left">
              <span class="batch-status-badge" :class="batch.status">
                {{ batchStatusLabel(batch.status) }}
              </span>
              <span class="batch-id mono">{{ batch.batch_id }}</span>
              <span class="batch-time mono">{{ formatDateTime(batch.created_at) }}</span>
            </div>

            <div class="history-head-right">
              <button
                v-if="batch.status === 'executed'"
                type="button"
                class="btn-undo-small"
                :disabled="store.isUndoingId === batch.batch_id"
                @click="handleUndo(batch.batch_id)"
              >
                <RotateCcw :size="12" />
                <span>{{ store.isUndoingId === batch.batch_id ? '撤回中...' : '一键撤回' }}</span>
              </button>
              <button
                type="button"
                class="link-btn"
                @click="expandedBatches[batch.batch_id] = !expandedBatches[batch.batch_id]"
              >
                {{ expandedBatches[batch.batch_id] ? '收起明细' : `查看 ${batch.steps.length} 条记录` }}
              </button>
            </div>
          </div>

          <p class="history-summary">{{ batch.summary }}</p>

          <div class="history-meta-line mono">
            <span>执行 {{ batch.approved_count }} 项</span>
            <span>·</span>
            <span>跳过 {{ batch.skipped_count }} 项</span>
            <template v-if="batch.reverted_at">
              <span>·</span>
              <span>撤回时间 {{ formatDateTime(batch.reverted_at) }}</span>
            </template>
          </div>

          <div v-if="batch.error_message" class="history-error">
            {{ batch.error_message }}
          </div>

          <!-- Expandable Step Audit Log -->
          <div v-if="expandedBatches[batch.batch_id]" class="steps-table">
            <div
              v-for="(step, idx) in batch.steps"
              :key="`${batch.batch_id}_${idx}`"
              class="step-row mono"
              :class="step.status"
            >
              <span class="step-op">{{ step.op_type }}</span>
              <span class="step-path">
                <template v-if="step.source">{{ step.source }} → {{ step.target }}</template>
                <template v-else>{{ step.target }}/</template>
              </span>
              <span v-if="step.file_hash" class="step-hash" :title="`SHA-256: ${step.file_hash}`">
                sha256:{{ step.file_hash.slice(0, 8) }}
              </span>
              <span class="step-status-pill" :class="step.status">{{ step.status }}</span>
            </div>
          </div>
        </article>
      </div>
    </div>
  </main>
</template>

<style scoped>
.diff-workspace {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: var(--bg-canvas);
}

.workspace-tabs-bar {
  padding: 10px 18px;
  border-bottom: 1px solid var(--border-default);
  background: var(--bg-surface);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-shrink: 0;
}

.segmented-tabs {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px;
  border-radius: var(--radius-md);
  background: var(--bg-subtle);
  border: 1px solid var(--border-subtle);
}

.seg-tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 11px;
  border-radius: 6px;
  border: none;
  background: transparent;
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 550;
  cursor: pointer;
  transition: all 0.14s ease;
}

.seg-tab:hover {
  color: var(--text-primary);
}

.seg-tab.active {
  background: var(--bg-surface);
  color: var(--text-primary);
  box-shadow: var(--shadow-xs);
}

.tab-pill {
  font-size: 10.5px;
  padding: 0 5px;
  height: 16px;
  line-height: 16px;
  border-radius: 999px;
  background: var(--bg-subtle);
  color: var(--text-secondary);
}

.tab-pill.interrupt {
  background: var(--l3-bg);
  color: var(--l3-text);
}

.stats-strip {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
}

.stat-pill {
  padding: 2px 7px;
  border-radius: 5px;
  font-weight: 600;
  border: 1px solid transparent;
}

.stat-pill.add {
  background: var(--diff-add-bg);
  color: var(--diff-add-text);
  border-color: var(--diff-add-border);
}

.stat-pill.move {
  background: var(--l1-bg);
  color: var(--l1-text);
  border-color: var(--l1-border);
}

.stat-pill.rename {
  background: var(--diff-rename-bg);
  color: var(--diff-rename-text);
  border-color: var(--diff-rename-border);
}

.stat-pill.archive {
  background: var(--diff-archive-bg);
  color: var(--diff-archive-text);
  border-color: var(--diff-archive-border);
}

.tab-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.plan-scroll-area,
.live-scroll-area,
.history-scroll-area {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 16px 18px 24px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

/* Plan Summary Card */
.plan-summary-card {
  padding: 12px 14px;
  border-radius: var(--radius-lg);
  background: var(--bg-surface);
  border: 1px solid var(--border-default);
  box-shadow: var(--shadow-xs);
}

.plan-summary-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}

.plan-title-group {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.plan-head-right {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.btn-reset-plan {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 24px;
  padding: 0 9px;
  border-radius: 6px;
  border: 1px solid var(--border-strong);
  background: var(--bg-canvas);
  color: var(--text-primary);
  font-size: 11px;
  font-weight: 550;
  cursor: pointer;
  transition: all 0.14s ease;
}

.btn-reset-plan:hover {
  border-color: var(--text-primary);
  background: var(--bg-hover);
}

.manual-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 10px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--l2-bg);
  color: var(--l2-text);
  border: 1px solid var(--l2-border);
  flex-shrink: 0;
}

.manual-badge.compact {
  padding: 0 5px;
  height: 17px;
  line-height: 17px;
  border-radius: 4px;
}

.after-tree-header {
  gap: 8px;
  flex-wrap: wrap;
}

.after-title-group {
  display: flex;
  align-items: center;
  gap: 6px;
}

.tree-toolbar-actions {
  display: flex;
  align-items: center;
  gap: 5px;
}

.tree-mini-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 23px;
  padding: 0 8px;
  border-radius: 5px;
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  color: var(--text-secondary);
  font-size: 11px;
  font-weight: 550;
  cursor: pointer;
  transition: all 0.13s ease;
}

.tree-mini-btn:hover {
  border-color: var(--border-strong);
  color: var(--text-primary);
  background: var(--bg-hover);
}

.tree-mini-btn.primary {
  border-color: var(--plan-border);
  background: var(--plan-bg);
  color: var(--plan-text);
}

.tree-mini-btn.primary:hover {
  border-color: var(--plan-text);
}

.new-folder-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-subtle);
}

.new-folder-input {
  flex: 1;
  min-width: 0;
  height: 25px;
  padding: 0 8px;
  border-radius: 5px;
  border: 1px solid var(--border-strong);
  background: var(--bg-surface);
  color: var(--text-primary);
  font-size: 11.5px;
  outline: none;
}

.new-folder-input:focus {
  border-color: var(--text-primary);
}

.mini-confirm-btn,
.mini-cancel-btn {
  height: 25px;
  padding: 0 9px;
  border-radius: 5px;
  font-size: 11px;
  font-weight: 550;
  cursor: pointer;
  flex-shrink: 0;
}

.mini-confirm-btn {
  border: 1px solid transparent;
  background: var(--cta-bg);
  color: var(--cta-text);
}

.mini-cancel-btn {
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  color: var(--text-muted);
}

.root-drop-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-surface);
  font-size: 11.5px;
  color: var(--text-secondary);
  transition: all 0.13s ease;
}

.root-drop-row.drag-active {
  background: var(--bg-canvas);
  border-bottom: 1px dashed var(--border-strong);
}

.root-drop-row.dropzone-active {
  background: var(--l1-bg);
  outline: 1.5px dashed var(--l1-text);
  outline-offset: -2px;
  color: var(--l1-text);
}

.root-drop-name {
  font-weight: 600;
  color: var(--text-primary);
}

.root-drop-hint {
  margin-left: auto;
  font-size: 10.5px;
  color: var(--text-faint);
}

.tree-row.draggable {
  cursor: grab;
}

.tree-row.draggable:active {
  cursor: grabbing;
}

.tree-row.is-dragging {
  opacity: 0.42;
}

.tree-row.dropzone-active {
  background: var(--l1-bg) !important;
  outline: 1.5px dashed var(--l1-text);
  outline-offset: -1.5px;
  border-radius: 4px;
}

.drag-grip {
  color: var(--text-faint);
  opacity: 0.4;
  transition: opacity 0.12s ease;
}

.tree-row.draggable:hover .drag-grip {
  opacity: 1;
  color: var(--text-secondary);
}

.drop-pill {
  margin-left: auto;
  font-size: 10px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--l1-text);
  color: #ffffff;
  flex-shrink: 0;
}

.inline-rename-input {
  flex: 1;
  min-width: 80px;
  height: 22px;
  padding: 0 6px;
  border-radius: 4px;
  border: 1.5px solid var(--text-primary);
  background: var(--bg-surface);
  color: var(--text-primary);
  font-size: 12px;
  outline: none;
}

.row-rename-trigger {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 4px;
  border: none;
  background: transparent;
  color: var(--text-muted);
  opacity: 0;
  cursor: pointer;
  flex-shrink: 0;
  transition: opacity 0.12s ease, background-color 0.12s ease;
}

.tree-row:hover .row-rename-trigger {
  opacity: 1;
}

.row-rename-trigger:hover {
  background: var(--bg-subtle);
  color: var(--text-primary);
}

.hitl-badge {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--l3-bg);
  color: var(--l3-text);
  border: 1px solid var(--l3-border);
}

.plan-id {
  font-size: 11px;
  color: var(--text-muted);
}

.zero-write-note {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11.5px;
  font-weight: 500;
  color: var(--plan-text);
}

.plan-summary-text {
  margin: 0;
  font-size: 13px;
  color: var(--text-primary);
  line-height: 1.55;
}

.plan-warnings {
  margin-top: 8px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--l3-bg);
  border: 1px solid var(--l3-border);
  color: var(--l3-text);
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.warning-line {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
}

/* Side-by-Side Trees */
.split-trees-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.tree-card {
  border-radius: var(--radius-lg);
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  overflow: hidden;
  display: flex;
  flex-direction: column;
  box-shadow: var(--shadow-xs);
}

.tree-card.after-card {
  border-color: var(--plan-border);
}

.tree-card-header {
  padding: 8px 12px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-canvas);
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.tree-card-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
}

.tree-card-sub {
  font-size: 11px;
  color: var(--text-muted);
}

.tree-list {
  max-height: 290px;
  overflow-y: auto;
  padding: 6px 0;
}

.tree-row {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 27px;
  padding-right: 10px;
  font-size: 12px;
  color: var(--text-primary);
  cursor: default;
  transition: background-color 0.1s ease;
}

.tree-row:hover {
  background: var(--bg-hover);
}

.tree-row.live {
  height: 31px;
  border-bottom: 1px solid var(--border-subtle);
}

.tree-row.live:last-child {
  border-bottom: none;
}

.tree-row.highlighted {
  outline: 1.5px solid var(--text-primary);
  outline-offset: -1.5px;
  background: var(--bg-hover);
}

.tree-row.diff-added,
.tree-row.diff-moved_to {
  background: var(--diff-add-bg);
}

.tree-row.diff-moved_from,
.tree-row.diff-renamed_from {
  background: var(--diff-remove-bg);
}

.tree-row.diff-moved_from .node-name,
.tree-row.diff-renamed_from .node-name {
  text-decoration: line-through;
  color: var(--diff-remove-text);
}

.tree-row.diff-renamed_to {
  background: var(--diff-rename-bg);
}

.tree-row.diff-archived_to {
  background: var(--diff-archive-bg);
}

.chevron-slot {
  width: 14px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--text-faint);
  flex-shrink: 0;
}

.node-icon {
  color: var(--text-muted);
  flex-shrink: 0;
}

.node-icon.folder {
  color: var(--l1-text);
}

.node-name {
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.diff-tag {
  font-size: 10px;
  font-weight: 600;
  padding: 0 5px;
  height: 17px;
  line-height: 17px;
  border-radius: 4px;
  flex-shrink: 0;
}

.diff-tag.added,
.diff-tag.moved_to {
  color: var(--diff-add-text);
  background: var(--bg-surface);
  border: 1px solid var(--diff-add-border);
}

.diff-tag.moved_from,
.diff-tag.renamed_from {
  color: var(--diff-remove-text);
  background: var(--bg-surface);
  border: 1px solid var(--diff-remove-border);
}

.diff-tag.renamed_to {
  color: var(--diff-rename-text);
  background: var(--bg-surface);
  border: 1px solid var(--diff-rename-border);
}

.diff-tag.archived_to {
  color: var(--diff-archive-text);
  background: var(--bg-surface);
  border: 1px solid var(--diff-archive-border);
}

.related-hint {
  margin-left: auto;
  font-size: 10.5px;
  color: var(--text-muted);
  max-width: 170px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.size-hint,
.live-row-meta {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-faint);
  flex-shrink: 0;
}

.ext-pill {
  font-size: 10px;
  padding: 0 5px;
  border-radius: 4px;
  background: var(--bg-subtle);
  color: var(--text-muted);
}

/* Granular Operations Checklist */
.ops-section {
  border-radius: var(--radius-lg);
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  overflow: hidden;
  box-shadow: var(--shadow-xs);
}

.ops-toolbar {
  padding: 10px 14px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-canvas);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.ops-title-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.ops-heading {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
}

.filter-pills {
  display: flex;
  align-items: center;
  gap: 4px;
}

.filter-pill {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  color: var(--text-muted);
  cursor: pointer;
}

.filter-pill.active {
  background: var(--cta-bg);
  color: var(--cta-text);
  border-color: var(--cta-bg);
}

.ops-search-box {
  position: relative;
  display: flex;
  align-items: center;
}

.search-icon {
  position: absolute;
  left: 8px;
  color: var(--text-faint);
  pointer-events: none;
}

.mini-search {
  height: 27px;
  width: 195px;
  padding: 0 8px 0 26px;
  border-radius: 6px;
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  color: var(--text-primary);
  font-size: 12px;
  outline: none;
}

.mini-search:focus {
  border-color: var(--text-primary);
}

.ops-list {
  display: flex;
  flex-direction: column;
}

.op-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border-subtle);
  transition: background-color 0.12s ease, opacity 0.12s ease;
}

.op-item:last-child {
  border-bottom: none;
}

.op-item:hover,
.op-item.highlighted {
  background: var(--bg-hover);
}

.op-item.unchecked {
  opacity: 0.5;
}

.op-check {
  padding-top: 1px;
}

.op-main {
  flex: 1;
  min-width: 0;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.op-top-line {
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;
}

.op-type-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 10.5px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 5px;
  border: 1px solid transparent;
}

.op-type-badge.mkdir {
  background: var(--diff-add-bg);
  color: var(--diff-add-text);
  border-color: var(--diff-add-border);
}

.op-type-badge.move {
  background: var(--l1-bg);
  color: var(--l1-text);
  border-color: var(--l1-border);
}

.op-type-badge.rename {
  background: var(--diff-rename-bg);
  color: var(--diff-rename-text);
  border-color: var(--diff-rename-border);
}

.op-type-badge.archive {
  background: var(--diff-archive-bg);
  color: var(--diff-archive-text);
  border-color: var(--diff-archive-border);
}

.evidence-badge {
  font-size: 10px;
  font-weight: 600;
  padding: 1px 5px;
  border-radius: 4px;
}

.evidence-badge.l1 {
  background: var(--l1-bg);
  color: var(--l1-text);
}
.evidence-badge.l2 {
  background: var(--l2-bg);
  color: var(--l2-text);
}
.evidence-badge.l3 {
  background: var(--l3-bg);
  color: var(--l3-text);
}

.op-paths {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  flex-wrap: wrap;
}

.path-src {
  color: var(--text-muted);
}

.arrow-sep {
  color: var(--text-faint);
}

.path-dst {
  color: var(--text-primary);
  font-weight: 550;
}

.op-size {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-faint);
}

.op-reason {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.45;
}

/* Sticky Approval Footer */
.approval-footer {
  padding: 12px 18px;
  border-top: 1px solid var(--border-default);
  background: var(--bg-surface);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-shrink: 0;
}

.footer-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.selection-summary {
  font-size: 12.5px;
  color: var(--text-secondary);
}

.link-btn {
  border: none;
  background: transparent;
  color: var(--l1-text);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  padding: 2px 5px;
  border-radius: 4px;
}

.link-btn:hover {
  background: var(--l1-bg);
}

.footer-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.btn-reject,
.btn-approve,
.btn-undo-banner,
.btn-undo-small {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border-radius: var(--radius-md);
  font-size: 12.5px;
  font-weight: 550;
  cursor: pointer;
  transition: all 0.14s ease;
}

.btn-reject {
  height: 34px;
  padding: 0 13px;
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  color: var(--text-secondary);
}

.btn-reject:hover:not(:disabled) {
  border-color: var(--danger-border);
  background: var(--danger-bg);
  color: var(--danger-text);
}

.btn-approve {
  height: 34px;
  padding: 0 15px;
  border: 1px solid transparent;
  background: var(--cta-bg);
  color: var(--cta-text);
}

.btn-approve:hover:not(:disabled) {
  background: var(--cta-bg-hover);
}

.btn-approve:disabled,
.btn-reject:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

/* Post-Execution Banner & Live Tree */
.post-exec-banner {
  padding: 12px 14px;
  border-radius: var(--radius-lg);
  background: var(--plan-bg);
  border: 1px solid var(--plan-border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.banner-left {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

.banner-icon {
  color: var(--plan-text);
  margin-top: 2px;
  flex-shrink: 0;
}

.banner-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--plan-text);
}

.banner-sub {
  font-size: 12px;
  color: var(--text-secondary);
  margin-top: 2px;
}

.btn-undo-banner {
  height: 32px;
  padding: 0 12px;
  border: 1px solid var(--plan-border);
  background: var(--bg-surface);
  color: var(--text-primary);
}

.btn-undo-banner:hover:not(:disabled) {
  border-color: var(--text-primary);
}

.live-tree-card {
  border-radius: var(--radius-lg);
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  overflow: hidden;
  box-shadow: var(--shadow-xs);
}

.live-tree-head {
  padding: 10px 14px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-canvas);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.live-title-group {
  display: flex;
  align-items: center;
  gap: 7px;
}

.live-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text-primary);
}

.live-root-tag {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 5px;
  background: var(--bg-subtle);
  color: var(--text-muted);
}

.live-tree-list {
  padding: 4px 0;
}

.empty-sandbox {
  padding: 48px 24px;
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
}

.empty-sandbox h4 {
  margin: 0;
  font-size: 14px;
  color: var(--text-primary);
}

.empty-sandbox p {
  margin: 0 0 6px;
  font-size: 12.5px;
  color: var(--text-muted);
  max-width: 440px;
  line-height: 1.6;
}

.empty-sandbox code {
  font-family: var(--font-mono);
  font-size: 11.5px;
  background: var(--bg-subtle);
  padding: 1px 5px;
  border-radius: 4px;
}

/* History Tab */
.history-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.history-card {
  padding: 14px 16px;
  border-radius: var(--radius-lg);
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  box-shadow: var(--shadow-xs);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.history-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.history-head-left,
.history-head-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.batch-status-badge {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid transparent;
}

.batch-status-badge.executed {
  background: var(--plan-bg);
  color: var(--plan-text);
  border-color: var(--plan-border);
}

.batch-status-badge.reverted {
  background: var(--bg-subtle);
  color: var(--text-muted);
  border-color: var(--border-default);
}

.batch-status-badge.rolled_back_on_error {
  background: var(--danger-bg);
  color: var(--danger-text);
  border-color: var(--danger-border);
}

.batch-id {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-primary);
}

.batch-time {
  font-size: 11px;
  color: var(--text-faint);
}

.btn-undo-small {
  height: 27px;
  padding: 0 10px;
  font-size: 11.5px;
  border: 1px solid var(--border-strong);
  background: var(--bg-surface);
  color: var(--text-primary);
}

.btn-undo-small:hover:not(:disabled) {
  border-color: var(--text-primary);
  background: var(--bg-hover);
}

.history-summary {
  margin: 0;
  font-size: 13px;
  color: var(--text-primary);
}

.history-meta-line {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 11px;
  color: var(--text-muted);
}

.history-error {
  padding: 8px 10px;
  border-radius: 6px;
  background: var(--danger-bg);
  border: 1px solid var(--danger-border);
  color: var(--danger-text);
  font-size: 12px;
}

.steps-table {
  margin-top: 4px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-subtle);
  background: var(--bg-canvas);
  overflow: hidden;
}

.step-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  font-size: 11.5px;
  border-bottom: 1px solid var(--border-subtle);
}

.step-row:last-child {
  border-bottom: none;
}

.step-row.skipped {
  opacity: 0.55;
}

.step-op {
  width: 56px;
  font-weight: 600;
  color: var(--l1-text);
  flex-shrink: 0;
}

.step-path {
  flex: 1;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.step-hash {
  font-size: 10.5px;
  color: var(--text-faint);
}

.step-status-pill {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--bg-subtle);
  color: var(--text-muted);
}

.step-status-pill.completed {
  background: var(--plan-bg);
  color: var(--plan-text);
}

.step-status-pill.failed {
  background: var(--danger-bg);
  color: var(--danger-text);
}

.icon-muted {
  color: var(--text-muted);
}

@media (max-width: 1160px) {
  .split-trees-grid {
    grid-template-columns: 1fr;
  }
}
</style>
