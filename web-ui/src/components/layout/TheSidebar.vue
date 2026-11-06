<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { 
  LayoutDashboard, 
  ListTodo, 
  Users, 
  Layers, 
  Terminal, 
  Settings2,
  ChevronRight
} from 'lucide-vue-next'
import { useWebSocket } from '@/composables/useWebSocket'
import { useCrawlerHealth } from '@/composables/useCrawlerHealth'
import { useInboxCount } from '@/composables/useInboxCount'
import { useI18n } from 'vue-i18n'
import type { CrawlerHealthLevel } from '@/types/health.d.ts'

const emit = defineEmits<{
  (event: 'navigate'): void
}>()
const router = useRouter()
const { isConnected } = useWebSocket()
const { overallLevel, issues, fetchHealth } = useCrawlerHealth()
const { count: inboxCount, fetchInboxCount } = useInboxCount()
const { t } = useI18n()

const navItems = computed(() => [
  { to: '/dashboard', label: t('sidebar.dashboard'), icon: LayoutDashboard },
  { to: '/tasks', label: t('sidebar.tasks'), icon: ListTodo },
  { to: '/accounts', label: t('sidebar.accounts'), icon: Users },
  { to: '/results', label: t('sidebar.results'), icon: Layers, badge: inboxCount.value > 0 ? inboxCount.value : 0 },
  { to: '/logs', label: t('sidebar.logs'), icon: Terminal },
  { to: '/settings', label: t('sidebar.settings'), icon: Settings2 },
])

const connectionLabel = computed(() => (
  isConnected.value ? t('sidebar.backendConnected') : t('sidebar.backendConnecting')
))
const connectionTone = computed(() =>
  isConnected.value
    ? 'bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.5)]'
    : 'bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,0.45)]'
)

const HEALTH_DOT_CLASS: Record<CrawlerHealthLevel, string> = {
  ok: 'bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.5)]',
  warning: 'bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,0.45)]',
  critical: 'bg-rose-500 shadow-[0_0_8px_rgba(244,63,94,0.5)]',
  unknown: 'bg-slate-300',
}
const HEALTH_TEXT_CLASS: Record<CrawlerHealthLevel, string> = {
  ok: 'text-emerald-600',
  warning: 'text-amber-600',
  critical: 'text-rose-600',
  unknown: 'text-slate-400',
}
// `overallLevel` is `ok` while the endpoint is missing / not loaded, so the row never alarms without data.
const healthLevel = computed<CrawlerHealthLevel>(() => overallLevel.value)
const healthIssueCount = computed(() => issues.value.length)

function goHealthOverview() {
  router.push({ name: 'Dashboard' })
  emit('navigate')
}

onMounted(() => {
  fetchHealth()
  fetchInboxCount()
})
</script>

<template>
  <nav class="space-y-1">
    <RouterLink
      v-for="item in navItems"
      :key="item.to"
      :to="item.to"
      v-slot="{ isActive }"
      class="group relative flex items-center px-4 py-3 rounded-xl transition-all duration-200 overflow-hidden"
      @click="emit('navigate')"
    >
      <!-- Active Background Effect -->
      <div 
        v-if="isActive" 
        class="absolute inset-0 bg-gradient-to-r from-primary/10 to-transparent z-0"
      ></div>
      <div 
        v-if="isActive" 
        class="absolute left-0 top-1/2 -translate-y-1/2 w-1 h-6 bg-primary rounded-r-full"
      ></div>

      <div class="relative z-10 flex items-center w-full">
        <component 
          :is="item.icon" 
          class="w-5 h-5 mr-3 transition-colors"
          :class="isActive ? 'text-primary' : 'text-slate-400 group-hover:text-slate-600'"
        />
        <span 
          class="text-sm font-bold transition-colors flex-grow"
          :class="isActive ? 'text-slate-900' : 'text-slate-500 group-hover:text-slate-700'"
        >
          {{ item.label }}
        </span>
        <span
          v-if="item.badge"
          class="mr-1 inline-flex min-w-[1.25rem] items-center justify-center rounded-full bg-primary px-1.5 py-0.5 text-[10px] font-bold leading-none text-white"
          :aria-label="t('sidebar.inboxPending', { count: item.badge })"
          :title="t('sidebar.inboxPending', { count: item.badge })"
        >{{ item.badge > 99 ? '99+' : item.badge }}</span>
        <ChevronRight 
          v-if="isActive"
          class="w-4 h-4 text-primary animate-in fade-in slide-in-from-left-2"
        />
      </div>
    </RouterLink>

    <!-- Support Section -->
    <div class="mt-12 px-4">
      <div class="rounded-2xl p-4 bg-slate-50/50 border border-slate-100 border-dashed">
         <p class="text-[10px] font-black text-slate-400 uppercase tracking-widest mb-2">{{ t('sidebar.systemStatus') }}</p>
         <div class="flex items-center gap-2">
            <div class="w-2 h-2 rounded-full" :class="connectionTone"></div>
            <span class="text-xs font-bold text-slate-600">{{ connectionLabel }}</span>
         </div>
         <button
            type="button"
            class="mt-2.5 -mx-1.5 flex w-[calc(100%+0.75rem)] items-start gap-2 rounded-lg px-1.5 py-1 text-left transition-colors hover:bg-white/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            @click="goHealthOverview"
         >
            <div class="mt-[5px] w-2 h-2 shrink-0 rounded-full" :class="HEALTH_DOT_CLASS[healthLevel]"></div>
            <div class="min-w-0 flex-1">
               <div class="flex items-center justify-between gap-2">
                  <span class="text-xs font-bold text-slate-600">{{ t('health.title') }}</span>
                  <span class="shrink-0 text-[11px] font-bold" :class="HEALTH_TEXT_CLASS[healthLevel]">
                     {{ t(`health.level.${healthLevel}`) }}
                  </span>
               </div>
               <p
                  v-if="healthLevel !== 'ok' && healthIssueCount > 0"
                  class="mt-0.5 text-[11px] font-medium leading-snug"
                  :class="HEALTH_TEXT_CLASS[healthLevel]"
               >
                  {{ t('health.tasksNeedAttention', healthIssueCount) }}
               </p>
            </div>
         </button>
      </div>
    </div>
  </nav>
</template>

<style scoped>
.router-link-active {
  background-color: transparent !important;
}
</style>
