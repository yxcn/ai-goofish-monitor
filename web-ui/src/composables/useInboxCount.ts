import { ref } from 'vue'
import * as resultsApi from '@/api/results'
import { HttpError } from '@/lib/http'
import { useWebSocket } from '@/composables/useWebSocket'

// 侧边栏徽标和结果页共用同一份计数，模块级状态 + 单个在途请求
const count = ref(0)
const byFile = ref<Record<string, number>>({})

let inFlight: Promise<void> | null = null
let pendingRefetch = false
let refreshTimer: ReturnType<typeof setTimeout> | null = null
let refreshDueAt = 0

/** 任务结束后后端还要写完结果，等一下再数 */
const REFRESH_AFTER_STOP_MS = 2000

function isSilentFailure(e: unknown): boolean {
  if (e instanceof HttpError) return e.status === 404
  return e instanceof TypeError
}

async function fetchInboxCount(): Promise<void> {
  if (inFlight) return inFlight
  inFlight = (async () => {
    try {
      const data = await resultsApi.getInboxCount()
      count.value = Number(data?.count ?? 0) || 0
      byFile.value = data?.files ?? {}
    } catch (e) {
      if (!isSilentFailure(e)) console.warn('[inbox] 读取待处理计数失败', e)
    } finally {
      inFlight = null
      if (pendingRefetch) {
        pendingRefetch = false
        void fetchInboxCount()
      }
    }
  })()
  return inFlight
}

function refreshNow() {
  if (inFlight) {
    pendingRefetch = true
    return
  }
  void fetchInboxCount()
}

function scheduleRefresh(delayMs: number) {
  const dueAt = Date.now() + delayMs
  if (refreshTimer && refreshDueAt >= dueAt) return
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshDueAt = dueAt
  refreshTimer = setTimeout(() => {
    refreshTimer = null
    refreshNow()
  }, delayMs)
}

export function useInboxCount() {
  const { on } = useWebSocket()

  on('results_updated', () => scheduleRefresh(500))
  on('task_status_changed', (data: { id: number; is_running: boolean } | undefined) => {
    if (data?.is_running) return
    scheduleRefresh(REFRESH_AFTER_STOP_MS)
  })

  return {
    count,
    byFile,
    fetchInboxCount,
    /** 本地改了状态后立刻重数 */
    refreshInboxCount: () => scheduleRefresh(0),
  }
}
