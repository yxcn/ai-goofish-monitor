import { ref, reactive, watch, onMounted, onScopeDispose, computed } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import type { ResultInsights, ResultItem, ReviewStatus, ReviewSummary } from '@/types/result.d.ts'
import * as resultsApi from '@/api/results'
import type { GetResultContentParams } from '@/api/results'
import { useWebSocket } from '@/composables/useWebSocket'
import { useInboxCount } from '@/composables/useInboxCount'
import * as tasksApi from '@/api/tasks'

export type ResultsTab = 'inbox' | 'contacted' | 'excluded' | 'bought' | 'all'
export const RESULT_TABS: ResultsTab[] = ['inbox', 'contacted', 'excluded', 'bought', 'all']

// 每个页签对应的后端筛选；「全部」沿用筛选栏里的推荐/屏蔽复选框
const TAB_QUERY: Record<ResultsTab, Pick<GetResultContentParams, 'review_status' | 'recommended'>> = {
  inbox: { review_status: 'new', recommended: true },
  contacted: { review_status: 'contacted' },
  excluded: { review_status: 'excluded' },
  bought: { review_status: 'bought' },
  all: {},
}

/** 收件箱里折叠的「不推荐」区最多拉这么多条（后端单次上限） */
export const NOT_RECOMMENDED_LIMIT = 100

const STORAGE_KEY_TAB = 'resultsTab'

type PersistedFilters = Required<Omit<GetResultContentParams, 'page' | 'limit' | 'review_status' | 'recommended'>>

export function useResults() {
  const { t } = useI18n()
  const route = useRoute()
  const inbox = useInboxCount()
  // State
  const files = ref<string[]>([])
  const selectedFile = ref<string | null>(null)
  const results = ref<ResultItem[]>([])
  const insights = ref<ResultInsights | null>(null)
  const summary = ref<ReviewSummary | null>(null)
  const totalItems = ref(0)
  const page = ref(1)
  // The backend caps `limit` at 100 per request.
  const limit = ref(100)
  const totalPages = computed(() => Math.max(1, Math.ceil(totalItems.value / limit.value)))

  function setPage(target: number) {
    const next = Math.min(Math.max(1, Math.trunc(target)), totalPages.value)
    if (next === page.value) return
    page.value = next
  }
  const blacklistKeywords = ref<string[]>([])
  const taskByKeyword = ref<Record<string, { id: number; task_name: string }>>({})
  const isFileOptionsReady = ref(false)
  const hasFetchedFiles = ref(false)
  const hasFetchedTasks = ref(false)
  const isSavingBlacklist = ref(false)
  const readyDelayMs = 200
  let readyTimer: ReturnType<typeof setTimeout> | null = null

  const STORAGE_KEY_FILTERS = 'resultFilters'

  function loadPersistedFilters(): PersistedFilters {
    const defaults: PersistedFilters = {
      recommended_only: false,
      ai_recommended_only: false,
      keyword_recommended_only: false,
      include_hidden: false,
      sort_by: 'crawl_time',
      sort_order: 'desc',
    }
    try {
      const saved = localStorage.getItem(STORAGE_KEY_FILTERS)
      if (saved) return { ...defaults, ...JSON.parse(saved) }
    } catch { /* ignore */ }
    return defaults
  }

  function loadPersistedTab(): ResultsTab {
    try {
      const saved = localStorage.getItem(STORAGE_KEY_TAB)
      if (saved && (RESULT_TABS as string[]).includes(saved)) return saved as ResultsTab
    } catch { /* ignore */ }
    return 'inbox'
  }

  const filters = reactive<PersistedFilters>(loadPersistedFilters())
  const tab = ref<ResultsTab>(loadPersistedTab())

  // 收件箱里折叠起来的「不推荐」商品，展开时才拉
  const showNotRecommended = ref(false)
  const secondaryResults = ref<ResultItem[]>([])
  const secondaryTotal = ref(0)
  const isSecondaryLoading = ref(false)

  const isLoading = ref(false)
  const error = ref<Error | null>(null)
  const { on } = useWebSocket()

  // Request sequencing: switching file/filters fires a new request before the
  // previous one resolves. Each fetch records its own sequence number and
  // discards its response when a newer request has been issued since.
  let resultsRequestSeq = 0
  let insightsRequestSeq = 0
  let blacklistRequestSeq = 0
  let summaryRequestSeq = 0
  let secondaryRequestSeq = 0

  function normalizeKeyword(value: string) {
    return value.trim().toLowerCase().replace(/\s+/g, '_')
  }

  function getKeywordFromFilename(filename: string) {
    return filename.replace(/_full_data\.jsonl$/i, '').toLowerCase()
  }

  /** 当前页签实际发给后端的参数；按状态分栏时不带筛选栏里的推荐/屏蔽复选框 */
  function buildQueryParams(): GetResultContentParams {
    const base: GetResultContentParams = { sort_by: filters.sort_by, sort_order: filters.sort_order }
    if (tab.value === 'all') {
      return {
        ...base,
        ai_recommended_only: filters.ai_recommended_only,
        keyword_recommended_only: filters.keyword_recommended_only,
        include_hidden: filters.include_hidden,
      }
    }
    return { ...base, ...TAB_QUERY[tab.value] }
  }

  // Methods
  async function fetchFiles() {
    try {
      const fileList = await resultsApi.getResultFiles()
      files.value = fileList
      // If a file is selected that no longer exists, reset it.
      // Otherwise, if nothing is selected, select the first file by default.
      if (selectedFile.value && fileList.includes(selectedFile.value)) {
        return
      }

      const lastSelected = localStorage.getItem('lastSelectedResultFile')
      if (lastSelected && fileList.includes(lastSelected)) {
        selectedFile.value = lastSelected
        return
      }

      selectedFile.value = fileList[0] || null
    } catch (e) {
      if (e instanceof Error) error.value = e
    } finally {
      hasFetchedFiles.value = true
      scheduleFileOptionsReady()
    }
  }

  async function fetchResults() {
    const seq = ++resultsRequestSeq
    const file = selectedFile.value
    if (!file) {
      results.value = []
      totalItems.value = 0
      isLoading.value = false
      return
    }

    isLoading.value = true
    error.value = null
    try {
      const data = await resultsApi.getResultContent(file, {
        ...buildQueryParams(),
        page: page.value,
        limit: limit.value,
      })
      if (seq !== resultsRequestSeq) return // superseded by a newer request
      results.value = data.items
      totalItems.value = data.total_items
      // e.g. the last item on the last page was hidden: fall back to the new last page.
      if (data.items.length === 0 && page.value > 1 && data.total_items > 0) {
        const lastPage = Math.max(1, Math.ceil(data.total_items / limit.value))
        if (lastPage < page.value) page.value = lastPage
      }
    } catch (e) {
      if (seq !== resultsRequestSeq) return
      if (e instanceof Error) error.value = e
      results.value = []
      totalItems.value = 0
    } finally {
      if (seq === resultsRequestSeq) isLoading.value = false
    }
  }

  async function fetchSecondary() {
    const seq = ++secondaryRequestSeq
    const file = selectedFile.value
    if (!file || tab.value !== 'inbox' || !showNotRecommended.value) {
      secondaryResults.value = []
      secondaryTotal.value = 0
      isSecondaryLoading.value = false
      return
    }
    isSecondaryLoading.value = true
    try {
      const data = await resultsApi.getResultContent(file, {
        sort_by: filters.sort_by,
        sort_order: filters.sort_order,
        review_status: 'new',
        recommended: false,
        page: 1,
        limit: NOT_RECOMMENDED_LIMIT,
      })
      if (seq !== secondaryRequestSeq) return
      secondaryResults.value = data.items
      secondaryTotal.value = data.total_items
    } catch (e) {
      if (seq !== secondaryRequestSeq) return
      if (e instanceof Error) error.value = e
      secondaryResults.value = []
      secondaryTotal.value = 0
    } finally {
      if (seq === secondaryRequestSeq) isSecondaryLoading.value = false
    }
  }

  async function fetchSummary() {
    const seq = ++summaryRequestSeq
    const file = selectedFile.value
    if (!file) {
      summary.value = null
      return
    }
    try {
      const data = await resultsApi.getReviewSummary(file)
      if (seq !== summaryRequestSeq) return
      summary.value = data
    } catch {
      // 旧后端没有这个接口：页签照常可用，只是不显示计数
      if (seq !== summaryRequestSeq) return
      summary.value = null
    }
  }

  async function fetchInsights() {
    const seq = ++insightsRequestSeq
    const file = selectedFile.value
    if (!file) {
      insights.value = null
      return
    }

    try {
      const data = await resultsApi.getResultInsights(file)
      if (seq !== insightsRequestSeq) return
      insights.value = data
    } catch (e) {
      if (seq !== insightsRequestSeq) return
      if (e instanceof Error) error.value = e
      insights.value = null
    }
  }

  async function fetchBlacklistRules() {
    const seq = ++blacklistRequestSeq
    const file = selectedFile.value
    if (!file) {
      blacklistKeywords.value = []
      return
    }

    try {
      const data = await resultsApi.getResultBlacklistRules(file)
      if (seq !== blacklistRequestSeq) return
      blacklistKeywords.value = data.keywords || []
    } catch (e) {
      if (seq !== blacklistRequestSeq) return
      if (e instanceof Error) error.value = e
      blacklistKeywords.value = []
    }
  }

  async function fetchTaskNameMap() {
    try {
      const tasks = await tasksApi.getAllTasks()
      const mapping: Record<string, { id: number; task_name: string }> = {}
      tasks.forEach((task) => {
        if (task.keyword) {
          mapping[normalizeKeyword(task.keyword)] = { id: task.id, task_name: task.task_name }
        }
      })
      taskByKeyword.value = mapping
    } catch (e) {
      if (e instanceof Error) error.value = e
    } finally {
      hasFetchedTasks.value = true
      scheduleFileOptionsReady()
    }
  }

  function scheduleFileOptionsReady() {
    if (isFileOptionsReady.value || !hasFetchedFiles.value || !hasFetchedTasks.value) return
    if (readyTimer) return
    readyTimer = setTimeout(() => {
      isFileOptionsReady.value = true
      readyTimer = null
    }, readyDelayMs)
  }

  // The ready delay may still be pending when the view unmounts (fast navigation away).
  onScopeDispose(() => {
    if (readyTimer) {
      clearTimeout(readyTimer)
      readyTimer = null
    }
  })

  /** 当前文件的列表、计数和折叠区一起刷新 */
  function refreshCurrentFile() {
    fetchResults()
    fetchSummary()
    if (showNotRecommended.value) fetchSecondary()
  }

  // Real-time updates
  on('results_updated', async () => {
    const oldFile = selectedFile.value
    await fetchFiles()
    // If the selected file remains the same, refresh its content (in case of append)
    // If it changed (e.g. from null to new file), the watcher will handle it.
    if (selectedFile.value && selectedFile.value === oldFile) {
      refreshCurrentFile()
      fetchInsights()
    }
  })

  on('tasks_updated', () => {
    fetchTaskNameMap()
  })

  // The backend only broadcasts `task_status_changed` ({id, is_running}), and only
  // when the state actually flips. A task that just stopped may have appended to
  // (or created) a result file, so refresh the file list and the current file's
  // content/insights. Filters and the page number are deliberately left alone.
  on('task_status_changed', async (data: { id: number; is_running: boolean } | undefined) => {
    if (data?.is_running) return
    const current = selectedFile.value
    await fetchFiles()
    // A changed selection (e.g. first file just appeared) is handled by the watchers.
    if (selectedFile.value && selectedFile.value === current) {
      refreshCurrentFile()
      fetchInsights()
    }
  })

  async function refreshResults() {
    const current = selectedFile.value
    await fetchFiles()
    if (selectedFile.value && selectedFile.value === current) {
      await fetchResults()
      await fetchSummary()
      if (showNotRecommended.value) await fetchSecondary()
      await fetchInsights()
      await fetchBlacklistRules()
    }
  }

  function exportSelectedResults() {
    if (!selectedFile.value) return
    resultsApi.downloadResultExport(selectedFile.value, { ...buildQueryParams() })
  }

  async function deleteSelectedFile(filename?: string) {
    const target = filename || selectedFile.value
    if (!target) return
    isLoading.value = true
    error.value = null
    try {
      await resultsApi.deleteResultFile(target)
      if (selectedFile.value === target) {
        const lastSelected = localStorage.getItem('lastSelectedResultFile')
        if (lastSelected === target) {
          localStorage.removeItem('lastSelectedResultFile')
        }
      }
      await fetchFiles()
      inbox.refreshInboxCount()
    } catch (e) {
      if (e instanceof Error) error.value = e
      throw e
    } finally {
      isLoading.value = false
    }
  }

  async function toggleItemBlock(item: ResultItem) {
    if (!selectedFile.value) return
    const itemId = item.商品信息?.商品ID
    if (!itemId) return
    const newStatus = item._status === 'hidden' ? 'active' : 'hidden'
    try {
      await resultsApi.updateItemStatus(selectedFile.value, itemId, newStatus)
      refreshCurrentFile()
      inbox.refreshInboxCount()
    } catch (e) {
      if (e instanceof Error) error.value = e
    }
  }

  /** 收件箱操作：已联系 / 排除 / 买到了 / 放回。失败时抛出，由页面提示。 */
  async function setReviewStatus(item: ResultItem, status: ReviewStatus) {
    const file = selectedFile.value
    const itemId = item.商品信息?.商品ID
    if (!file || !itemId) return
    await resultsApi.updateReviewStatus(file, itemId, status)
    await Promise.all([
      fetchResults(),
      fetchSummary(),
      showNotRecommended.value ? fetchSecondary() : Promise.resolve(),
    ])
    inbox.refreshInboxCount()
  }

  /**
   * 买到了：标记商品，可选把所在任务停掉（买到就没必要再每小时抓了）。
   * 标记失败抛出；停任务失败不抛，放在返回值里由页面提示。
   */
  async function markBought(item: ResultItem, disableTask: boolean): Promise<{ taskDisabled: boolean; taskError: Error | null }> {
    await setReviewStatus(item, 'bought')
    const task = selectedTask.value
    if (!disableTask || !task) return { taskDisabled: false, taskError: null }
    try {
      await tasksApi.updateTask(task.id, { enabled: false })
      return { taskDisabled: true, taskError: null }
    } catch (e) {
      return { taskDisabled: false, taskError: e instanceof Error ? e : new Error(String(e)) }
    }
  }

  async function saveBlacklistRules(keywords: string[]) {
    if (!selectedFile.value) return
    isSavingBlacklist.value = true
    error.value = null
    try {
      const data = await resultsApi.updateResultBlacklistRules(selectedFile.value, keywords)
      blacklistKeywords.value = data.keywords || []
      refreshCurrentFile()
      await fetchInsights()
      inbox.refreshInboxCount()
    } catch (e) {
      if (e instanceof Error) error.value = e
      throw e
    } finally {
      isSavingBlacklist.value = false
    }
  }

  function setTab(next: ResultsTab) {
    if (!RESULT_TABS.includes(next) || next === tab.value) return
    tab.value = next
  }

  // Watchers
  watch(filters, (val) => {
    localStorage.setItem(STORAGE_KEY_FILTERS, JSON.stringify(val))
  }, { deep: true })
  watch(tab, (val) => {
    localStorage.setItem(STORAGE_KEY_TAB, val)
    showNotRecommended.value = false
  })
  watch([selectedFile, filters, tab], () => {
    if (page.value !== 1) {
      // The page watcher below performs the single refetch.
      page.value = 1
      return
    }
    fetchResults()
  }, { deep: true })
  watch(page, fetchResults)
  // 排序变了，展开着的「不推荐」区也要跟着重排
  watch([() => filters.sort_by, () => filters.sort_order], () => {
    if (showNotRecommended.value) fetchSecondary()
  })
  watch(showNotRecommended, (shown) => {
    if (shown) fetchSecondary()
    else {
      secondaryResults.value = []
      secondaryTotal.value = 0
    }
  })
  watch(selectedFile, () => {
    showNotRecommended.value = false
    fetchSummary()
    fetchInsights()
    fetchBlacklistRules()
  })
  watch(selectedFile, (value) => {
    if (value) localStorage.setItem('lastSelectedResultFile', value)
  })
  watch(
    [() => route.query.file, files],
    ([routeFile, currentFiles]) => {
      if (typeof routeFile !== 'string') return
      if (currentFiles.includes(routeFile)) {
        selectedFile.value = routeFile
      }
    },
    { immediate: true }
  )

  const fileOptions = computed(() =>
    files.value.map((file) => {
      const keyword = getKeywordFromFilename(file)
      const taskName = taskByKeyword.value[keyword]?.task_name
      return {
        value: file,
        taskName: taskName || t('common.unnamed'),
        label: t('results.filters.taskNameLabel', {
          task: taskName || t('common.unnamed'),
        }),
      }
    })
  )

  /** 当前结果文件对应的任务（id + 名称），找不到为 null */
  const selectedTask = computed(() => {
    if (!selectedFile.value) return null
    return taskByKeyword.value[getKeywordFromFilename(selectedFile.value)] ?? null
  })

  // Lifecycle
  onMounted(() => {
    fetchFiles()
    fetchTaskNameMap()
  })

  return {
    files,
    selectedFile,
    results,
    insights,
    summary,
    totalItems,
    page,
    totalPages,
    setPage,
    filters,
    tab,
    setTab,
    showNotRecommended,
    secondaryResults,
    secondaryTotal,
    isSecondaryLoading,
    isLoading,
    error,
    fetchFiles, // Expose to allow manual refresh
    refreshResults,
    exportSelectedResults,
    deleteSelectedFile,
    toggleItemBlock,
    setReviewStatus,
    markBought,
    blacklistKeywords,
    isSavingBlacklist,
    saveBlacklistRules,
    fileOptions,
    isFileOptionsReady,
    selectedTask,
  }
}
