<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Inbox, MessageCircle, Ban, ShoppingBag, Layers, ChevronDown } from 'lucide-vue-next'
import { useResults, NOT_RECOMMENDED_LIMIT, type ResultsTab } from '@/composables/useResults'
import type { ResultItem, ReviewStatus } from '@/types/result.d.ts'
import ResultsFilterBar from '@/components/results/ResultsFilterBar.vue'
import ResultsGrid from '@/components/results/ResultsGrid.vue'
import ResultsInsightsPanel from '@/components/results/ResultsInsightsPanel.vue'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { toast } from '@/components/ui/toast'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

const { t } = useI18n()

const {
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
} = useResults()

const isDeleteDialogOpen = ref(false)
const isBlacklistDialogOpen = ref(false)
const blacklistDraft = ref('')
const gridContainer = ref<HTMLElement | null>(null)

// 买到了：确认框里可以顺手把任务停掉
const isBoughtDialogOpen = ref(false)
const pendingBoughtItem = ref<ResultItem | null>(null)
const disableTaskChecked = ref(true)
const isSubmittingBought = ref(false)

async function goToPage(target: number) {
  const previous = page.value
  setPage(target)
  if (page.value === previous) return
  await nextTick()
  gridContainer.value?.scrollIntoView({ block: 'start', behavior: 'smooth' })
}

const selectedTaskLabel = computed(() => {
  if (!selectedFile.value || fileOptions.value.length === 0) return null
  const match = fileOptions.value.find((option) => option.value === selectedFile.value)
  if (!match) return null
  return match.taskName || null
})

const deleteConfirmText = computed(() => {
  return selectedTaskLabel.value
    ? t('results.filters.deleteDialogWithTask', { task: selectedTaskLabel.value })
    : t('results.filters.deleteDialogFallback')
})

interface TabDef { value: ResultsTab; label: string; icon: typeof Inbox; count: number | null }
const tabDefs = computed<TabDef[]>(() => [
  { value: 'inbox', label: t('results.tabs.inbox'), icon: Inbox, count: summary.value?.inbox ?? null },
  { value: 'contacted', label: t('results.tabs.contacted'), icon: MessageCircle, count: summary.value?.contacted ?? null },
  { value: 'excluded', label: t('results.tabs.excluded'), icon: Ban, count: summary.value?.excluded ?? null },
  { value: 'bought', label: t('results.tabs.bought'), icon: ShoppingBag, count: summary.value?.bought ?? null },
  { value: 'all', label: t('results.tabs.all'), icon: Layers, count: summary.value?.total ?? null },
])

const emptyText = computed(() => {
  switch (tab.value) {
    case 'inbox': return t('results.inbox.empty')
    case 'contacted': return t('results.inbox.emptyContacted')
    case 'excluded': return t('results.inbox.emptyExcluded')
    case 'bought': return t('results.inbox.emptyBought')
    default: return t('results.grid.empty')
  }
})

// 收件箱底下那行「另有 N 条不推荐」：计数接口没有时展开后再用列表的 total
const notRecommendedCount = computed(() => summary.value?.not_recommended ?? (showNotRecommended.value ? secondaryTotal.value : null))
const showNotRecommendedRow = computed(() => tab.value === 'inbox' && !!selectedFile.value && (notRecommendedCount.value === null || notRecommendedCount.value > 0))

function handleTabChange(value: string | number) {
  setTab(String(value) as ResultsTab)
}

function reviewBadgeLabel(status: ReviewStatus) {
  if (status === 'contacted') return t('results.review.badgeContacted')
  if (status === 'bought') return t('results.review.badgeBought')
  return t('results.review.badgeExcluded')
}

async function handleReview(item: ResultItem, status: ReviewStatus) {
  if (status === 'bought') {
    pendingBoughtItem.value = item
    disableTaskChecked.value = !!selectedTask.value
    isBoughtDialogOpen.value = true
    return
  }
  try {
    await setReviewStatus(item, status)
    toast({
      title: status === 'new'
        ? t('results.review.restored')
        : t('results.review.updated', { status: reviewBadgeLabel(status) }),
    })
  } catch (e) {
    toast({ title: t('results.review.updateFailed'), description: (e as Error).message, variant: 'destructive' })
  }
}

async function handleConfirmBought() {
  const item = pendingBoughtItem.value
  if (!item) return
  isSubmittingBought.value = true
  try {
    const outcome = await markBought(item, disableTaskChecked.value)
    const taskName = selectedTask.value?.task_name ?? ''
    toast({
      title: t('results.review.updated', { status: t('results.review.badgeBought') }),
      description: outcome.taskDisabled ? t('results.review.taskDisabled', { task: taskName }) : undefined,
    })
    if (outcome.taskError) {
      toast({ title: t('results.review.taskDisableFailed'), description: outcome.taskError.message, variant: 'destructive' })
    }
    isBoughtDialogOpen.value = false
    pendingBoughtItem.value = null
  } catch (e) {
    toast({ title: t('results.review.updateFailed'), description: (e as Error).message, variant: 'destructive' })
  } finally {
    isSubmittingBought.value = false
  }
}

function openDeleteDialog() {
  if (!selectedFile.value) {
    toast({
      title: t('results.filters.noResultToDelete'),
      variant: 'destructive',
    })
    return
  }
  isDeleteDialogOpen.value = true
}

function openBlacklistDialog() {
  if (!selectedFile.value) {
    toast({
      title: t('results.filters.noResultSelected'),
      variant: 'destructive',
    })
    return
  }
  blacklistDraft.value = blacklistKeywords.value.join('\n')
  isBlacklistDialogOpen.value = true
}

function handleExportResults() {
  if (!selectedFile.value) {
    toast({
      title: t('results.filters.noResultToExport'),
      variant: 'destructive',
    })
    return
  }
  exportSelectedResults()
}

async function handleDeleteResults() {
  if (!selectedFile.value) return
  try {
    await deleteSelectedFile(selectedFile.value)
    toast({ title: t('results.filters.resultDeleted') })
  } catch (e) {
    toast({
      title: t('results.filters.deleteFailed'),
      description: (e as Error).message,
      variant: 'destructive',
    })
  } finally {
    isDeleteDialogOpen.value = false
  }
}

function parseBlacklistKeywords(input: string) {
  return input
    .split(/[\n,，]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

async function handleSaveBlacklistRules() {
  try {
    await saveBlacklistRules(parseBlacklistKeywords(blacklistDraft.value))
    toast({ title: t('results.filters.blacklistSaved') })
    isBlacklistDialogOpen.value = false
  } catch (e) {
    toast({
      title: t('results.filters.blacklistSaveFailed'),
      description: (e as Error).message,
      variant: 'destructive',
    })
  }
}
</script>

<template>
  <div>
    <h1 class="text-2xl font-bold text-gray-800 mb-6">
      {{ t('results.title') }}
    </h1>

    <div v-if="error" class="app-alert-error mb-4" role="alert">
      <strong class="font-bold">{{ t('common.error') }}</strong>
      <span class="block sm:inline">{{ error.message }}</span>
    </div>

    <ResultsFilterBar
      :files="files"
      :file-options="fileOptions"
      :is-ready="isFileOptionsReady"
      :show-visibility-filters="tab === 'all'"
      v-model:selectedFile="selectedFile"
      v-model:aiRecommendedOnly="filters.ai_recommended_only"
      v-model:keywordRecommendedOnly="filters.keyword_recommended_only"
      v-model:includeHidden="filters.include_hidden"
      v-model:sortBy="filters.sort_by"
      v-model:sortOrder="filters.sort_order"
      :is-loading="isLoading"
      @refresh="refreshResults"
      @manage-blacklist="openBlacklistDialog"
      @export="handleExportResults"
      @delete="openDeleteDialog"
    />

    <!-- 收件箱 / 已联系 / 已排除 / 已买到 / 全部 -->
    <Tabs :model-value="tab" class="mb-4" @update:model-value="handleTabChange">
      <TabsList class="flex w-full flex-nowrap justify-start gap-1 overflow-x-auto rounded-xl bg-slate-100 p-1">
        <TabsTrigger
          v-for="def in tabDefs"
          :key="def.value"
          :value="def.value"
          class="shrink-0 gap-1.5"
        >
          <component :is="def.icon" class="w-3.5 h-3.5" />
          {{ def.label }}
          <span
            v-if="def.count !== null"
            class="rounded-full px-1.5 py-0.5 text-[10px] font-bold leading-none"
            :class="def.value === 'inbox' && def.count > 0 ? 'bg-primary text-white' : 'bg-slate-200/80 text-slate-600'"
          >{{ def.count }}</span>
        </TabsTrigger>
      </TabsList>
    </Tabs>
    <p v-if="tab === 'inbox'" class="-mt-2 mb-4 text-xs text-slate-400">{{ t('results.inbox.hint') }}</p>

    <div ref="gridContainer" class="scroll-mt-6">
      <ResultsGrid
        :results="results"
        :is-loading="isLoading"
        :empty-text="emptyText"
        @toggle-block="toggleItemBlock"
        @review="handleReview"
      />
    </div>

    <nav
      v-if="totalPages > 1"
      class="mt-6 flex flex-wrap items-center justify-center gap-3"
      :aria-label="t('results.pagination.label')"
    >
      <Button
        variant="outline"
        size="sm"
        :disabled="isLoading || page <= 1"
        @click="goToPage(page - 1)"
      >
        {{ t('results.pagination.prev') }}
      </Button>
      <span class="text-sm text-slate-600" aria-live="polite">
        {{ t('results.pagination.status', { page, total: totalPages, count: totalItems }) }}
      </span>
      <Button
        variant="outline"
        size="sm"
        :disabled="isLoading || page >= totalPages"
        @click="goToPage(page + 1)"
      >
        {{ t('results.pagination.next') }}
      </Button>
    </nav>

    <!-- 收件箱里折叠的不推荐商品 -->
    <section v-if="showNotRecommendedRow" class="mt-6">
      <button
        type="button"
        class="app-surface flex w-full items-center justify-between px-4 py-3 text-left text-sm transition-colors hover:bg-slate-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        :aria-expanded="showNotRecommended"
        @click="showNotRecommended = !showNotRecommended"
      >
        <span class="font-semibold text-slate-600">
          {{ notRecommendedCount !== null ? t('results.inbox.notRecommended', { count: notRecommendedCount }) : t('results.tabs.all') }}
        </span>
        <span class="flex items-center gap-1 text-xs text-slate-400">
          {{ showNotRecommended ? t('results.inbox.hideNotRecommended') : t('results.inbox.showNotRecommended') }}
          <ChevronDown class="w-4 h-4 transition-transform" :class="{ 'rotate-180': showNotRecommended }" />
        </span>
      </button>
      <div v-if="showNotRecommended" class="mt-4">
        <ResultsGrid
          :results="secondaryResults"
          :is-loading="isSecondaryLoading"
          :empty-text="t('results.inbox.notRecommendedEmpty')"
          :skeleton-count="4"
          @toggle-block="toggleItemBlock"
          @review="handleReview"
        />
        <p v-if="secondaryTotal > NOT_RECOMMENDED_LIMIT" class="mt-3 text-center text-xs text-slate-400">
          {{ t('results.inbox.truncated', { limit: NOT_RECOMMENDED_LIMIT }) }}
        </p>
      </div>
    </section>

    <div class="mt-8">
      <ResultsInsightsPanel :insights="insights" :selected-task-label="selectedTaskLabel" />
    </div>

    <Dialog v-model:open="isBoughtDialogOpen">
      <DialogContent class="sm:max-w-[440px]">
        <DialogHeader>
          <DialogTitle>{{ t('results.review.boughtDialogTitle') }}</DialogTitle>
          <DialogDescription>
            {{ t('results.review.boughtDialogDescription', { title: pendingBoughtItem?.商品信息?.商品标题 ?? '' }) }}
          </DialogDescription>
        </DialogHeader>
        <div v-if="selectedTask" class="flex items-start space-x-2">
          <Checkbox
            id="disable-task-after-bought"
            :model-value="disableTaskChecked"
            @update:modelValue="(value) => (disableTaskChecked = value === true)"
          />
          <Label for="disable-task-after-bought" class="cursor-pointer text-sm leading-5 text-slate-700">
            {{ t('results.review.disableTaskOption', { task: selectedTask.task_name }) }}
          </Label>
        </div>
        <DialogFooter>
          <Button variant="outline" :disabled="isSubmittingBought" @click="isBoughtDialogOpen = false">{{ t('common.cancel') }}</Button>
          <Button :disabled="isSubmittingBought" @click="handleConfirmBought">
            {{ t('results.review.confirmBought') }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog v-model:open="isDeleteDialogOpen">
      <DialogContent class="sm:max-w-[420px]">
        <DialogHeader>
          <DialogTitle>{{ t('results.filters.deleteDialogTitle') }}</DialogTitle>
          <DialogDescription>
            {{ deleteConfirmText }}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" @click="isDeleteDialogOpen = false">{{ t('common.cancel') }}</Button>
          <Button variant="destructive" :disabled="isLoading" @click="handleDeleteResults">
            {{ t('results.filters.confirmDelete') }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog v-model:open="isBlacklistDialogOpen">
      <DialogContent class="sm:max-w-[520px]">
        <DialogHeader>
          <DialogTitle>{{ t('results.filters.blacklistDialogTitle') }}</DialogTitle>
          <DialogDescription>
            {{ t('results.filters.blacklistDialogDescription') }}
          </DialogDescription>
        </DialogHeader>
        <div class="space-y-2">
          <label class="text-sm font-medium text-slate-700">
            {{ t('results.filters.blacklistRulesLabel') }}
          </label>
          <Textarea
            v-model="blacklistDraft"
            class="min-h-[180px]"
            :placeholder="t('results.filters.blacklistRulesPlaceholder')"
          />
          <p class="text-xs leading-5 text-slate-500">
            {{ t('results.filters.blacklistRulesHint') }}
          </p>
        </div>
        <DialogFooter>
          <Button variant="outline" @click="isBlacklistDialogOpen = false">{{ t('common.cancel') }}</Button>
          <Button :disabled="isSavingBlacklist" @click="handleSaveBlacklistRules">
            {{ t('results.filters.confirmBlacklistSave') }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>
