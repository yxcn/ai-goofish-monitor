import { computed, ref } from 'vue'
import * as healthApi from '@/api/health'
import { HttpError } from '@/lib/http'
import { useWebSocket } from '@/composables/useWebSocket'
import type {
  CrawlerHealthLevel,
  CrawlerHealthSnapshot,
  CrawlerTaskHealth,
} from '@/types/health.d.ts'

// Module-level state: the sidebar, the dashboard banner and the tasks table all
// show the same snapshot, so they share one copy and one in-flight request.
const health = ref<CrawlerHealthSnapshot | null>(null)
const isLoading = ref(false)
const error = ref<Error | null>(null)

let inFlight: Promise<void> | null = null
let pendingRefetch = false
let refreshTimer: ReturnType<typeof setTimeout> | null = null
let refreshDueAt = 0

/** The backend writes a task's health summary only after the run ends; give it a moment. */
const REFRESH_AFTER_STOP_MS = 2000

/**
 * "The endpoint is not there / not reachable" is not an error to the user: the
 * backend may simply not ship the endpoint yet. The UI then behaves as if all is ok.
 */
function isSilentFailure(e: unknown): boolean {
  if (e instanceof HttpError) return e.status === 404
  // fetch() rejects with a TypeError on a network failure.
  return e instanceof TypeError
}

async function fetchHealth(): Promise<void> {
  if (inFlight) return inFlight
  isLoading.value = true
  inFlight = (async () => {
    try {
      health.value = await healthApi.getCrawlerHealth()
      error.value = null
    } catch (e) {
      health.value = null
      error.value = isSilentFailure(e) || !(e instanceof Error) ? null : e
    } finally {
      isLoading.value = false
      inFlight = null
      if (pendingRefetch) {
        pendingRefetch = false
        void fetchHealth()
      }
    }
  })()
  return inFlight
}

/** A refresh that must not be swallowed by a request already in flight. */
function refreshNow() {
  if (inFlight) {
    pendingRefetch = true
    return
  }
  void fetchHealth()
}

/**
 * Coalesces refresh requests from several components (and several stopped
 * tasks) into one fetch. A later due time wins so the summary of the last task
 * to stop is included.
 */
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

const ACTIONABLE_LEVELS: ReadonlySet<CrawlerHealthLevel> = new Set(['warning', 'critical'])
const LEVEL_ORDER: Record<CrawlerHealthLevel, number> = { critical: 0, warning: 1, unknown: 2, ok: 3 }

const healthByTaskName = computed(() => {
  const map = new Map<string, CrawlerTaskHealth>()
  for (const task of health.value?.tasks ?? []) {
    map.set(task.task_name, task)
  }
  return map
})

/** Tasks the user has to act on (warning / critical), most severe first. `unknown` (never ran) is not an issue. */
const issues = computed(() =>
  (health.value?.tasks ?? [])
    .filter((task) => ACTIONABLE_LEVELS.has(task.level))
    .sort((a, b) => LEVEL_ORDER[a.level] - LEVEL_ORDER[b.level])
)

/** `ok` while there is no snapshot (endpoint missing / not loaded yet). */
const overallLevel = computed(() => health.value?.overall_level ?? 'ok')

export function useCrawlerHealth() {
  const { on } = useWebSocket()

  // Same event the results/dashboard composables use. Only a task that just
  // stopped can change its health summary.
  on('task_status_changed', (data: { id: number; is_running: boolean } | undefined) => {
    if (data?.is_running) return
    scheduleRefresh(REFRESH_AFTER_STOP_MS)
  })
  // Task created / renamed / deleted: entries are keyed by task name.
  on('tasks_updated', () => {
    scheduleRefresh(0)
  })

  return {
    health,
    isLoading,
    error,
    fetchHealth,
    healthByTaskName,
    issues,
    overallLevel,
  }
}
