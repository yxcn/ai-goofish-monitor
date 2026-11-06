import { ref, onMounted, onUnmounted } from 'vue'
import * as logsApi from '@/api/logs'
import { t } from '@/i18n'

export function useLogs() {
  const logs = ref('')
  const currentPos = ref(0)
  const currentTaskId = ref<number | null>(null)
  const historyOffset = ref(0)
  const hasMoreHistory = ref(false)
  const isFetchingHistory = ref(false)
  const isAutoRefresh = ref(true)
  const isLoading = ref(false)
  const error = ref<Error | null>(null)

  let refreshInterval: number | null = null
  const MAX_LOG_CHARS = 200_000
  const TRIM_LOG_CHARS = 150_000
  const TRIM_NOTICE = t('logs.trimmedNotice')

  // Single in-flight lock shared by the incremental poll (`fetchLogs`) and the
  // tail/history loaders, so two requests can never interleave their writes to
  // `logs` / `currentPos`. `requestToken` is bumped whenever the local state is
  // reset (task switch, clear); a response whose token no longer matches belongs
  // to a previous task/state and is discarded.
  let inFlight = false
  let requestToken = 0

  function appendLogs(content: string) {
    if (!content) return
    logs.value += content
    // Prevent unbounded growth that can freeze the UI.
    if (logs.value.length > MAX_LOG_CHARS) {
      const tail = logs.value.slice(-TRIM_LOG_CHARS)
      logs.value = `${TRIM_NOTICE}\n${tail}`
    }
  }

  /** Discard any in-flight request and release the lock so a new one can start. */
  function invalidateInFlight() {
    requestToken += 1
    inFlight = false
    isLoading.value = false
    isFetchingHistory.value = false
  }

  function acquire(): { token: number; taskId: number } | null {
    if (inFlight) return null
    const taskId = currentTaskId.value
    if (taskId === null) return null
    inFlight = true
    return { token: requestToken, taskId }
  }

  /** True when the request identified by `token` is still the current one. */
  function isCurrent(token: number, taskId: number) {
    return token === requestToken && currentTaskId.value === taskId
  }

  function release(token: number) {
    // A stale request must not clobber the lock of a newer one.
    if (token !== requestToken) return
    inFlight = false
    isLoading.value = false
    isFetchingHistory.value = false
  }

  async function fetchLogs() {
    const lease = acquire()
    if (!lease) return
    const { token, taskId } = lease
    isLoading.value = true
    try {
      const data = await logsApi.getLogs(currentPos.value, taskId)
      if (!isCurrent(token, taskId)) return
      if (data.new_pos < currentPos.value) {
        // Log file rotated or cleared.
        logs.value = ''
      }
      if (data.new_content) {
        appendLogs(data.new_content)
      }
      currentPos.value = data.new_pos
    } catch (e) {
      if (!isCurrent(token, taskId)) return
      if (e instanceof Error) error.value = e
    } finally {
      release(token)
    }
  }

  /** Returns false when skipped (no task selected or another request in flight). */
  async function loadLatest(limitLines: number = 50): Promise<boolean> {
    const lease = acquire()
    if (!lease) return false
    const { token, taskId } = lease
    isFetchingHistory.value = true
    try {
      const data = await logsApi.getLogTail(taskId, 0, limitLines)
      if (!isCurrent(token, taskId)) return false
      logs.value = data.content || ''
      historyOffset.value = data.next_offset
      hasMoreHistory.value = data.has_more
      currentPos.value = data.new_pos
    } catch (e) {
      if (!isCurrent(token, taskId)) return false
      if (e instanceof Error) error.value = e
    } finally {
      release(token)
    }
    return true
  }

  /** Returns false when skipped (nothing more, no task, or another request in flight). */
  async function loadPrevious(limitLines: number = 50): Promise<boolean> {
    if (!hasMoreHistory.value) return false
    const lease = acquire()
    if (!lease) return false
    const { token, taskId } = lease
    isFetchingHistory.value = true
    try {
      const data = await logsApi.getLogTail(taskId, historyOffset.value, limitLines)
      if (!isCurrent(token, taskId)) return false
      if (data.content) {
        logs.value = logs.value ? `${data.content}\n${logs.value}` : data.content
      }
      historyOffset.value = data.next_offset
      hasMoreHistory.value = data.has_more
      currentPos.value = data.new_pos
    } catch (e) {
      if (!isCurrent(token, taskId)) return false
      if (e instanceof Error) error.value = e
    } finally {
      release(token)
    }
    return true
  }

  function resetState() {
    logs.value = ''
    currentPos.value = 0
    historyOffset.value = 0
    hasMoreHistory.value = false
  }

  async function clearLogs() {
    try {
      const taskId = currentTaskId.value
      if (taskId === null) return
      await logsApi.clearLogs(taskId)
      if (currentTaskId.value !== taskId) return
      // An incremental poll started before the clear would re-append old content.
      invalidateInFlight()
      resetState()
    } catch (e) {
      if (e instanceof Error) error.value = e
      throw e
    }
  }

  function startTimer() {
    if (refreshInterval) return
    refreshInterval = window.setInterval(fetchLogs, 2000)
  }

  function stopTimer() {
    if (refreshInterval) {
      clearInterval(refreshInterval)
      refreshInterval = null
    }
  }

  function startAutoRefresh() {
    isAutoRefresh.value = true
    if (refreshInterval) return
    fetchLogs() // Fetch immediately
    startTimer()
  }

  function stopAutoRefresh() {
    stopTimer()
    isAutoRefresh.value = false
  }

  function toggleAutoRefresh() {
    if (isAutoRefresh.value) {
      stopAutoRefresh()
    } else {
      startAutoRefresh()
    }
  }

  function setTaskId(taskId: number | null) {
    if (currentTaskId.value === taskId) return
    // Stop polling, drop whatever is in flight for the previous task, reset,
    // then resume polling. The timer is restarted without an immediate fetch so
    // that the caller's `loadLatest()` is the first request for the new task.
    stopTimer()
    invalidateInFlight()
    currentTaskId.value = taskId
    resetState()
    if (isAutoRefresh.value && taskId !== null) {
      startTimer()
    }
  }

  onMounted(() => {
    startAutoRefresh()
  })

  onUnmounted(() => {
    stopTimer()
    invalidateInFlight()
  })

  return {
    logs,
    isAutoRefresh,
    isLoading, // Not strictly used for polling to avoid flickering
    isFetchingHistory,
    hasMoreHistory,
    error,
    fetchLogs,
    clearLogs,
    toggleAutoRefresh,
    setTaskId,
    loadLatest,
    loadPrevious
  }
}
