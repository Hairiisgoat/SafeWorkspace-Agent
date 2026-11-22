/**
 * SafeWorkspace Agent Frontend API Client & SSE Stream Parser
 * Mirrors backend/app/schemas.py data contracts 1:1.
 */

export type ProbeLevel = 'L1' | 'L2' | 'L3' | 'PLAN'
export type OperationType = 'mkdir' | 'move' | 'rename' | 'archive'
export type DiffStatus =
  | 'unchanged'
  | 'added'
  | 'removed'
  | 'moved_from'
  | 'moved_to'
  | 'renamed_from'
  | 'renamed_to'
  | 'archived_to'

export type TraceEventType =
  | 'user_message'
  | 'run_started'
  | 'thinking'
  | 'tool_start'
  | 'tool_end'
  | 'security_blocked'
  | 'plan_interrupt'
  | 'assistant_message'
  | 'execution_complete'
  | 'error'
  | 'done'

export interface LLMConfigOverride {
  api_key?: string | null
  base_url?: string | null
  model_name?: string | null
}

export interface WorkspaceConfigUpdateRequest {
  sandbox_root?: string | null
  api_key?: string | null
  base_url?: string | null
  model_name?: string | null
}

export interface WorkspaceConfigResponse {
  sandbox_root: string
  sandbox_exists: boolean
  model_name: string
  base_url: string
  has_api_key: boolean
  api_key_masked: string
  file_count: number
  dir_count: number
  total_size_bytes: number
  recent_workspaces: string[]
  persisted: boolean
}

export interface WorkspaceDialogResponse {
  selected_path: string | null
  cancelled: boolean
  valid: boolean
  error: string | null
}

export interface FileTreeNode {
  name: string
  path: string
  type: 'file' | 'dir'
  size: number
  extension?: string | null
  modified_at?: string | null
  diff_status: DiffStatus
  operation_id?: string | null
  related_path?: string | null
  children: FileTreeNode[]
}

export interface FileOperation {
  id: string
  op_type: OperationType
  source?: string | null
  target: string
  reason: string
  probe_evidence: 'L1' | 'L2' | 'L3'
  category?: string | null
  size_bytes: number
  manual_adjusted?: boolean
}

export interface OrganizationPlan {
  plan_id: string
  thread_id: string
  summary: string
  created_at: string
  operations: FileOperation[]
  before_tree: FileTreeNode
  after_tree: FileTreeNode
  warnings: string[]
  stats: Record<string, number>
}

export interface ExecutionStepLog {
  operation_id: string
  op_type: OperationType
  source?: string | null
  target: string
  status: 'completed' | 'skipped' | 'failed' | 'reverted'
  created_dirs: string[]
  file_hash?: string | null
  error?: string | null
}

export interface ExecutionBatchRecord {
  batch_id: string
  plan_id: string
  thread_id: string
  summary: string
  status: 'executed' | 'reverted' | 'rolled_back_on_error' | 'cancelled'
  created_at: string
  reverted_at?: string | null
  approved_count: number
  skipped_count: number
  steps: ExecutionStepLog[]
  error_message?: string | null
}

export interface AgentRunRequest {
  instruction: string
  thread_id?: string | null
  llm_config?: LLMConfigOverride | null
}

export interface ConfirmPlanRequest {
  thread_id: string
  plan_id: string
  approved: boolean
  approved_operation_ids: string[]
  operations?: FileOperation[] | null
  user_comment?: string | null
}

export interface ConfirmPlanResponse {
  thread_id: string
  status: 'executed' | 'cancelled' | 'rolled_back_on_error'
  message: string
  batch?: ExecutionBatchRecord | null
  updated_tree: FileTreeNode
}

export interface UndoResponse {
  batch_id: string
  status: 'reverted'
  message: string
  batch: ExecutionBatchRecord
  updated_tree: FileTreeNode
}

export interface SeedDemoResponse {
  created_files: string[]
  skipped_files: string[]
  updated_tree: FileTreeNode
}

export interface TraceEvent {
  event_id: string
  thread_id: string
  timestamp: string
  type: TraceEventType
  probe_level?: ProbeLevel | null
  tool_name?: string | null
  tool_args?: Record<string, unknown> | null
  summary: string
  detail?: unknown
}

const API_BASE = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '')

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(`${API_BASE}${path}`, {
      headers: {
        'Content-Type': 'application/json',
        ...(init?.headers || {}),
      },
      ...init,
    })
  } catch (err) {
    if (err instanceof TypeError && /fetch/i.test(err.message)) {
      throw new Error(
        '无法连接本地服务 (Failed to fetch)，请确认 Backend (:8000) 与 Frontend (:5173) 终端窗口仍在运行。',
      )
    }
    throw err
  }

  if (!res.ok) {
    let message = `HTTP ${res.status}`
    try {
      const errBody = await res.json()
      if (errBody?.detail) {
        message = typeof errBody.detail === 'string' ? errBody.detail : JSON.stringify(errBody.detail)
      }
    } catch {
      const text = await res.text()
      if (text) message = text
    }
    throw new Error(message)
  }

  return (await res.json()) as T
}

export const apiClient = {
  getConfig(): Promise<WorkspaceConfigResponse> {
    return requestJson<WorkspaceConfigResponse>('/api/config')
  },

  updateConfig(payload: WorkspaceConfigUpdateRequest): Promise<WorkspaceConfigResponse> {
    return requestJson<WorkspaceConfigResponse>('/api/config', {
      method: 'PUT',
      body: JSON.stringify(payload),
    })
  },

  selectWorkspaceDialog(): Promise<WorkspaceDialogResponse> {
    return requestJson<WorkspaceDialogResponse>('/api/workspace/select-dialog', {
      method: 'POST',
    })
  },

  getWorkspaceTree(maxDepth = 6): Promise<FileTreeNode> {
    return requestJson<FileTreeNode>(`/api/workspace/tree?max_depth=${maxDepth}`)
  },

  seedDemoWorkspace(): Promise<SeedDemoResponse> {
    return requestJson<SeedDemoResponse>('/api/workspace/seed-demo', {
      method: 'POST',
    })
  },

  confirmPlan(payload: ConfirmPlanRequest): Promise<ConfirmPlanResponse> {
    return requestJson<ConfirmPlanResponse>('/api/workspace/execute', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },

  getHistory(): Promise<ExecutionBatchRecord[]> {
    return requestJson<ExecutionBatchRecord[]>('/api/history')
  },

  undoBatch(batchId: string): Promise<UndoResponse> {
    return requestJson<UndoResponse>(`/api/history/${encodeURIComponent(batchId)}/undo`, {
      method: 'POST',
    })
  },

  /**
   * Stream POST /api/agent/run/stream using Fetch ReadableStream + SSE frame parser.
   * Properly handles CRLF (`\r\n\r\n`) and LF (`\n\n`) delimiters from sse-starlette.
   */
  async streamAgentRun(
    payload: AgentRunRequest,
    handlers: {
      onEvent: (event: TraceEvent) => void
      onError?: (err: Error) => void
      onClose?: () => void
    },
    signal?: AbortSignal,
  ): Promise<void> {
    const res = await fetch(`${API_BASE}/api/agent/run/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
      },
      body: JSON.stringify(payload),
      signal,
    })

    if (!res.ok) {
      let detail = `SSE connection failed (HTTP ${res.status})`
      try {
        const body = await res.json()
        if (body?.detail) detail = String(body.detail)
      } catch {
        // ignore parse fallback
      }
      throw new Error(detail)
    }

    if (!res.body) {
      throw new Error('ReadableStream not supported in this browser environment.')
    }

    const reader = res.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''

    const flushFrame = (rawFrame: string) => {
      const lines = rawFrame.split(/\r?\n/)
      const dataLines: string[] = []
      for (const line of lines) {
        if (!line || line.startsWith(':')) continue
        if (line.startsWith('data:')) {
          dataLines.push(line.slice(5).trimStart())
        }
      }
      if (dataLines.length === 0) return
      const rawJson = dataLines.join('\n')
      try {
        const parsed = JSON.parse(rawJson) as TraceEvent
        handlers.onEvent(parsed)
      } catch (parseErr) {
        handlers.onError?.(
          parseErr instanceof Error ? parseErr : new Error(`Invalid SSE JSON frame: ${rawJson}`),
        )
      }
    }

    try {
      while (true) {
        const { value, done } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        // Split complete SSE frames on blank lines (\r\n\r\n or \n\n)
        const parts = buffer.split(/\r?\n\r?\n/)
        buffer = parts.pop() || ''
        for (const frame of parts) {
          if (frame.trim()) {
            flushFrame(frame)
          }
        }
      }

      const trailing = decoder.decode()
      if (trailing) buffer += trailing
      if (buffer.trim()) {
        flushFrame(buffer)
      }
    } finally {
      reader.releaseLock()
      handlers.onClose?.()
    }
  },
}
