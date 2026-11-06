<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { RouterLink } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ArrowRight, Lightbulb, OctagonAlert, TriangleAlert } from 'lucide-vue-next'
import { Badge } from '@/components/ui/badge'
import { useCrawlerHealth } from '@/composables/useCrawlerHealth'
import { formatDateTime, formatRelativeTimeFromNow } from '@/i18n'
import type { CrawlerHealthLevel, CrawlerTaskHealth } from '@/types/health.d.ts'

const { t, te } = useI18n()
const { health, issues, overallLevel, fetchHealth } = useCrawlerHealth()

// Nothing is rendered when everything is fine or there is no data at all.
const isVisible = computed(() => overallLevel.value !== 'ok' && issues.value.length > 0)
const isCritical = computed(() => overallLevel.value === 'critical')

const bannerClass = computed(() =>
  isCritical.value
    ? 'border-rose-200 bg-rose-50/90 text-rose-900'
    : 'border-amber-200 bg-amber-50/90 text-amber-900'
)
const iconClass = computed(() => (isCritical.value ? 'text-rose-500' : 'text-amber-500'))
const dividerClass = computed(() => (isCritical.value ? 'divide-rose-200/70' : 'divide-amber-200/70'))

const dotClass = (level: CrawlerHealthLevel) => (level === 'critical' ? 'bg-rose-500' : 'bg-amber-500')
const badgeClass = (level: CrawlerHealthLevel) =>
  level === 'critical'
    ? 'border-rose-200 bg-white/70 text-rose-700'
    : 'border-amber-200 bg-white/70 text-amber-700'

// Unknown categories (a newer backend) fall back to the level word / no action.
function categoryTitle(item: CrawlerTaskHealth) {
  const key = `health.category.${item.category}`
  return te(key) ? t(key) : t(`health.level.${item.level}`)
}

function suggestedAction(item: CrawlerTaskHealth) {
  const key = `health.action.${item.category}`
  return te(key) ? t(key) : null
}

function hasMeta(item: CrawlerTaskHealth) {
  return Boolean(item.last_run_at) || item.consecutive_failures > 0 || Boolean(item.paused_until)
}

function formatPausedUntil(value: string) {
  return formatDateTime(value, { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

onMounted(fetchHealth)
</script>

<template>
  <section
    v-if="isVisible"
    role="alert"
    :class="['rounded-2xl border px-5 py-4 shadow-sm backdrop-blur-sm', bannerClass]"
  >
    <div class="flex items-start gap-3">
      <component
        :is="isCritical ? OctagonAlert : TriangleAlert"
        :class="['mt-0.5 h-5 w-5 shrink-0', iconClass]"
      />
      <div class="min-w-0 flex-1">
        <div class="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <h2 class="text-sm font-black tracking-tight">{{ t('health.bannerTitle') }}</h2>
          <span v-if="health?.generated_at" class="text-[11px] font-medium opacity-70">
            {{ t('health.generatedAt', { time: formatRelativeTimeFromNow(health.generated_at) }) }}
          </span>
        </div>

        <ul :class="['mt-3 divide-y', dividerClass]">
          <li v-for="item in issues" :key="item.task_id" class="py-3 first:pt-0 last:pb-0">
            <div class="flex flex-wrap items-center gap-2">
              <span :class="['h-2 w-2 shrink-0 rounded-full', dotClass(item.level)]"></span>
              <span class="text-sm font-black">{{ item.task_name }}</span>
              <Badge variant="outline" :class="['h-5 px-2 text-[10px] font-bold', badgeClass(item.level)]">
                {{ categoryTitle(item) }}
              </Badge>
              <RouterLink
                :to="{ name: 'Logs', query: { task: String(item.task_id) } }"
                class="ml-auto inline-flex items-center gap-1 text-xs font-bold underline-offset-2 hover:underline"
              >
                {{ t('health.viewLogs') }}
                <ArrowRight class="h-3 w-3" />
              </RouterLink>
            </div>
            <p v-if="item.message" class="mt-1.5 text-sm leading-relaxed">{{ item.message }}</p>
            <p v-if="suggestedAction(item)" class="mt-1.5 flex items-start gap-1.5 text-xs font-medium opacity-80">
              <Lightbulb class="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{{ suggestedAction(item) }}</span>
            </p>
            <p v-if="hasMeta(item)" class="mt-1.5 flex flex-wrap gap-x-3 text-[11px] opacity-60">
              <span v-if="item.last_run_at">
                {{ t('health.lastRun', { time: formatRelativeTimeFromNow(item.last_run_at) }) }}
              </span>
              <span v-if="item.consecutive_failures > 0">
                {{ t('health.consecutiveFailures', { count: item.consecutive_failures }) }}
              </span>
              <span v-if="item.paused_until">
                {{ t('health.pausedUntil', { time: formatPausedUntil(item.paused_until) }) }}
              </span>
            </p>
          </li>
        </ul>
      </div>
    </div>
  </section>
</template>
