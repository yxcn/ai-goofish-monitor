<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { CriteriaEntry, ResultItem, ReviewStatus } from '@/types/result.d.ts'
import {
  Card,
  CardContent,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import Badge from '@/components/ui/badge/Badge.vue'
import { ExternalLink, TrendingUp, TrendingDown, Info, User, Clock, CheckCircle2, XCircle, AlertCircle, EyeOff, Eye, ChevronDown, Tag, HelpCircle, MessageCircle, ShoppingBag, Ban, Undo2 } from 'lucide-vue-next'
import { formatDateTime } from '@/i18n'

interface Props {
  item: ResultItem
  /** 页面级「详细模式」：true 时所有折叠块默认展开；卡片上的开关只覆盖自己 */
  details?: boolean
}

const props = defineProps<Props>()
const emit = defineEmits<{
  (e: 'toggle-block', item: ResultItem): void
  (e: 'review', item: ResultItem, status: ReviewStatus): void
}>()
const { t, te } = useI18n()

// computed 而不是解构常量：屏蔽/取消屏蔽后列表以同一商品 ID 复用组件实例，props.item 会换成新对象
const info = computed(() => props.item.商品信息)
const seller = computed(() => props.item.卖家信息)
const ai = computed(() => props.item.ai_analysis)
const priceInsight = computed(() => props.item.price_insight)

// 紧凑 / 详细：默认只留做决定要看的东西（价格、不符项、风险信号、贵贱），其余点「详情」再展开
const detailsOverride = ref<boolean | null>(null)
const detailsOpen = computed(() => detailsOverride.value ?? props.details ?? false)
function toggleDetails() {
  detailsOverride.value = !detailsOpen.value
}

// SKIP_AI_ANALYSIS 模式：is_recommended 仍为 true 但 analysis_source 为 'skipped'，不算 AI 推荐（不打精选、不显示匹配度）
const isSkipped = computed(() => ai.value?.analysis_source === 'skipped')
const isRecommended = computed(() => ai.value?.is_recommended === true && !isSkipped.value)
// 和通知消息同一套规则：reason 以“有条件推荐”开头的推荐单独标注
const isConditional = computed(() => typeof ai.value?.reason === 'string' && ai.value.reason.trimStart().startsWith('有条件推荐'))
const recommendationStatus = computed(() => {
  if (isSkipped.value) return { label: t('results.card.analysisSkipped'), color: 'bg-slate-400', icon: Info, text: 'text-slate-600', bg: 'bg-slate-50' }
  if (ai.value?.is_recommended === true && isConditional.value) return { label: t('results.card.conditionalRecommend'), color: 'bg-amber-500', icon: CheckCircle2, text: 'text-amber-700', bg: 'bg-amber-50' }
  if (ai.value?.is_recommended === true) return { label: t('results.card.strongRecommend'), color: 'bg-emerald-500', icon: CheckCircle2, text: 'text-emerald-600', bg: 'bg-emerald-50' }
  if (ai.value?.is_recommended === false) return { label: t('results.card.notRecommended'), color: 'bg-rose-500', icon: XCircle, text: 'text-rose-600', bg: 'bg-rose-50' }
  return { label: t('results.card.pending'), color: 'bg-amber-500', icon: AlertCircle, text: 'text-amber-600', bg: 'bg-amber-50' }
})

const imageUrl = computed(() => info.value.商品图片列表?.[0] || info.value.商品主图链接 || '')
const crawlTime = computed(() => (props.item.爬取时间
  ? formatDateTime(props.item.爬取时间, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
  : t('common.unknown')))
const matchScore = computed(() => ai.value?.value_score ?? 0)
// 商品页上的那几项：标签（包邮/验货宝）、想要、浏览、收藏、发货地、发布时间；占位符（NaN、-）不显示
const listingTags = computed(() => Array.from(new Set((info.value.商品标签 ?? []).map((tag) => String(tag ?? '').trim()).filter(Boolean))))
const toCount = (value: unknown): number | null => {
  if (value === null || value === undefined || value === '') return null
  const parsed = Number(String(value).trim())
  return Number.isFinite(parsed) ? Math.round(parsed) : null
}
const wantsCount = computed(() => toCount(info.value['“想要”人数']))
const viewCount = computed(() => toCount(info.value.浏览量))
const favoriteCount = computed(() => toCount(info.value.收藏数))
const publishTime = computed(() => {
  const raw = String(info.value.发布时间 ?? '').trim()
  if (!raw || raw === '未知时间') return ''
  return raw.replace(/^\d{4}-/, '')
})
// 发货地区是省份，详情页还给了卖家城市；两者都有且不重复时合成“山东 淄博”
const region = computed(() => {
  const rawProvince = String(info.value.发货地区 ?? '').trim()
  const province = rawProvince && rawProvince !== '地区未知' ? rawProvince : ''
  const city = String(info.value.卖家所在城市 ?? '').trim()
  if (province && city && !province.includes(city) && !city.includes(province)) return `${province} ${city}`
  return city || province
})
// 一行事实：紧凑模式只留想要数、地区、发布时间；浏览/收藏/最近活跃/回复率放到详情里
interface Fact { key: string; text: string; detail: boolean }
const facts = computed<Fact[]>(() => {
  const list: Fact[] = []
  if (wantsCount.value !== null) list.push({ key: 'wants', text: t('results.card.wants', { count: wantsCount.value }), detail: false })
  if (viewCount.value !== null) list.push({ key: 'views', text: t('results.card.views', { count: viewCount.value }), detail: true })
  if (favoriteCount.value !== null) list.push({ key: 'favorites', text: t('results.card.favorites', { count: favoriteCount.value }), detail: true })
  if (region.value) list.push({ key: 'region', text: region.value, detail: false })
  if (publishTime.value) list.push({ key: 'published', text: t('results.card.published', { time: publishTime.value }), detail: false })
  const lastActive = String(info.value.卖家最近活跃 ?? '').trim()
  const replyRate = String(info.value.卖家24小时回复率 ?? '').trim()
  const replyInterval = String(info.value.卖家平均回复时长 ?? '').trim()
  if (lastActive) list.push({ key: 'active', text: lastActive, detail: true })
  if (replyRate) list.push({ key: 'replyRate', text: t('results.card.replyRate', { rate: replyRate }), detail: true })
  if (replyInterval) list.push({ key: 'replyIn', text: t('results.card.replyIn', { time: replyInterval }), detail: true })
  const aiModel = String(ai.value?.ai_model ?? '').trim()
  if (aiModel) list.push({ key: 'aiModel', text: t('results.card.analyzedBy', { model: aiModel }), detail: true })
  return list.filter((fact) => detailsOpen.value || !fact.detail)
})
// 划线原价：占位符（暂无）或和现价相同时不显示
const originalPrice = computed(() => {
  const raw = String(info.value.商品原价 ?? '').trim()
  if (!raw || ['暂无', '-', 'N/A'].includes(raw)) return ''
  const digits = (text: string) => text.replace(/[^\d.]/g, '')
  return digits(raw) && digits(raw) === digits(String(info.value.当前售价 ?? '')) ? '' : raw
})
const hasListingFacts = computed(() => listingTags.value.length > 0 || facts.value.length > 0)
const isHidden = computed(() => props.item._effective_hidden === true || props.item._status === 'hidden')
const isRuleHidden = computed(() => props.item._hidden_reason === 'rule')
const canToggleBlock = computed(() => props.item._hidden_reason !== 'rule' && props.item._hidden_reason !== 'expired')
const hiddenLabel = computed(() => {
  if (props.item._hidden_reason === 'rule') return t('results.card.blacklisted')
  if (props.item._hidden_reason === 'expired') return t('results.card.expired')
  return t('results.card.hidden')
})

const expanded = ref(false)

// 逐项核对标签：AI 对每条硬性条件的 PASS / FAIL / NEEDS_MANUAL_CHECK，否决项排最前
type ChipState = 'fail' | 'check' | 'pass' | 'unknown'
const CHIP_ORDER: Record<ChipState, number> = { fail: 0, check: 1, pass: 2, unknown: 3 }
const CHIP_CLASS: Record<ChipState, string> = {
  fail: 'border-rose-200 bg-rose-50 text-rose-700',
  check: 'border-amber-200 bg-amber-50 text-amber-700',
  pass: 'border-emerald-200 bg-emerald-50 text-emerald-700',
  unknown: 'border-slate-200 bg-slate-50 text-slate-500',
}
const CHIP_ICON: Record<ChipState, typeof XCircle> = { fail: XCircle, check: AlertCircle, pass: CheckCircle2, unknown: HelpCircle }
function normalizeChipState(raw: unknown): ChipState {
  const value = String(raw ?? '').trim().toUpperCase()
  if (!value) return 'unknown'
  if (['PASS', 'PASSED', 'OK', '通过', '符合'].includes(value)) return 'pass'
  if (['FAIL', 'FAILED', '不通过', '不符合', '否决'].includes(value)) return 'fail'
  if (/NEED|CHECK|MANUAL|UNKNOWN|VERIFY|确认|待/.test(value)) return 'check'
  return 'unknown'
}
interface CriteriaChip { key: string; label: string; short: string; state: ChipState; comment: string; evidence: string }
const criteriaChips = computed<CriteriaChip[]>(() => {
  const raw = ai.value?.criteria_analysis
  if (!raw || typeof raw !== 'object') return []
  return Object.entries(raw)
    .filter(([, entry]) => entry && typeof entry === 'object')
    .map(([key, entry]) => {
      const detail = entry as CriteriaEntry
      const i18nKey = `results.criteria.${key}`
      return {
        key,
        label: te(i18nKey) ? t(i18nKey) : key.replace(/_/g, ' '),
        short: String(detail.short ?? '').trim(),
        state: normalizeChipState(detail.status),
        comment: String(detail.comment ?? '').trim(),
        evidence: String(detail.evidence ?? '').trim(),
      }
    })
    .sort((a, b) => CHIP_ORDER[a.state] - CHIP_ORDER[b.state])
})
const hasFail = computed(() => criteriaChips.value.some((chip) => chip.state === 'fail'))
const passCount = computed(() => criteriaChips.value.filter((chip) => chip.state === 'pass').length)
// 紧凑模式下通过项折成一个「其余 N 项通过」，红黄灰的才摊开
const passExpanded = ref(false)
const visibleChips = computed(() => (detailsOpen.value || passExpanded.value
  ? criteriaChips.value
  : criteriaChips.value.filter((chip) => chip.state !== 'pass')))
const hiddenPassCount = computed(() => criteriaChips.value.length - visibleChips.value.length)
const activeChipKey = ref<string | null>(null)
const activeChip = computed(() => visibleChips.value.find((chip) => chip.key === activeChipKey.value) ?? null)
function toggleChip(key: string) {
  activeChipKey.value = activeChipKey.value === key ? null : key
}
// 理由文字：有否决项或者根本没有标签（老记录）时才常驻，否则放进详情
const showReason = computed(() => detailsOpen.value || criteriaChips.value.length === 0 || hasFail.value)

// 收件箱处理状态和可做的操作
const reviewStatus = computed<ReviewStatus>(() => props.item._review_status ?? 'new')
const reviewBadge = computed(() => {
  switch (reviewStatus.value) {
    case 'contacted': return { label: t('results.review.badgeContacted'), cls: 'bg-sky-500/90 text-white' }
    case 'bought': return { label: t('results.review.badgeBought'), cls: 'bg-emerald-600/90 text-white' }
    case 'excluded': return { label: t('results.review.badgeExcluded'), cls: 'bg-slate-700/80 text-white' }
    default: return null
  }
})
interface ReviewAction { status: ReviewStatus; label: string; icon: typeof MessageCircle; cls: string }
const ACTIONS: Record<Exclude<ReviewStatus, 'new'> | 'restore', () => ReviewAction> = {
  contacted: () => ({ status: 'contacted', label: t('results.review.markContacted'), icon: MessageCircle, cls: 'border-sky-200 bg-sky-50 text-sky-700 hover:bg-sky-100' }),
  bought: () => ({ status: 'bought', label: t('results.review.markBought'), icon: ShoppingBag, cls: 'border-emerald-200 bg-emerald-50 text-emerald-700 hover:bg-emerald-100' }),
  excluded: () => ({ status: 'excluded', label: t('results.review.markExcluded'), icon: Ban, cls: 'border-slate-200 bg-white text-slate-500 hover:bg-slate-50' }),
  restore: () => ({ status: 'new', label: t('results.review.restore'), icon: Undo2, cls: 'border-slate-200 bg-white text-slate-500 hover:bg-slate-50' }),
}
const reviewActions = computed<ReviewAction[]>(() => {
  switch (reviewStatus.value) {
    case 'new': return [ACTIONS.contacted(), ACTIONS.bought(), ACTIONS.excluded()]
    case 'contacted': return [ACTIONS.bought(), ACTIONS.excluded(), ACTIONS.restore()]
    default: return [ACTIONS.restore()]
  }
})

// 卖家画像：程序按固定规则算出的摘要；信号常驻（⚠️ 在前），结论和明细在详情里
const profile = computed(() => (props.item.卖家画像 && typeof props.item.卖家画像 === 'object' ? props.item.卖家画像 : null))
const profileOverride = ref<boolean | null>(null)
const profileOpen = computed(() => profileOverride.value ?? detailsOpen.value)
const isWarningSignal = (signal: string) => signal.startsWith('⚠️')
const profileSignals = computed(() => (profile.value?.信号 ?? [])
  .map((s) => String(s ?? '').trim())
  .filter(Boolean)
  .sort((a, b) => Number(isWarningSignal(b)) - Number(isWarningSignal(a))))
// 紧凑模式整块只占三行：标题一行 + 最多 3 个信号（多出的折成 +N），展开才看全
const MAX_COMPACT_SIGNALS = 3
const visibleSignals = computed(() => (profileOpen.value ? profileSignals.value : profileSignals.value.slice(0, MAX_COMPACT_SIGNALS)))
const hiddenSignalCount = computed(() => profileSignals.value.length - visibleSignals.value.length)
const profileRows = computed(() => {
  const current = profile.value
  if (!current) return []
  const rows: Array<{ key: string; label: string; value: string }> = [
    { key: 'identity', label: t('results.card.profileIdentity'), value: String(current.身份 ?? '') },
    { key: 'activity', label: t('results.card.profileActivity'), value: String(current.活跃 ?? '') },
    { key: 'trades', label: t('results.card.profileTrades'), value: String(current.交易 ?? '') },
    { key: 'history', label: t('results.card.profileHistory'), value: String(current.历史 ?? '') },
    { key: 'reputation', label: t('results.card.profileReputation'), value: String(current.口碑 ?? '') },
  ]
  return rows.filter((row) => row.value && row.value !== '暂无')
})

// 页面级开关切换时，卡片自己的覆盖都清掉，跟着页面走
watch(() => props.details, () => {
  detailsOverride.value = null
  profileOverride.value = null
  passExpanded.value = false
})

// 贵贱判断：后端先比成交参考价，样本不足再比挂牌中位价；都不够就不下结论
const fmtPrice = (value: number | null | undefined) => (value === null || value === undefined ? '—' : `¥${Math.round(value)}`)
const priceVerdict = computed(() => {
  const key = priceInsight.value?.price_verdict_key
  if (!key || key === 'insufficient') return null
  const basis = priceInsight.value?.price_verdict_basis === 'sold' ? t('results.card.basisSold') : t('results.card.basisListing')
  const pct = priceInsight.value?.price_diff_percent ?? 0
  const detail = Math.abs(pct) < 1
    ? t('results.card.vsReferenceEqual', { basis })
    : t(pct < 0 ? 'results.card.vsReferenceBelow' : 'results.card.vsReferenceAbove', { basis, pct: Math.abs(Math.round(pct)) })
  const style = key === 'good_deal'
    ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
    : key === 'pricey'
      ? 'border-rose-200 bg-rose-50 text-rose-700'
      : 'border-slate-200 bg-slate-50 text-slate-600'
  return { label: t(`results.card.verdict.${key}`), detail, style }
})
const soldReference = computed(() => priceInsight.value?.sold_reference ?? null)
// 紧凑模式下三个参考价挤成一行，用两字短标签
const priceLine = computed(() => {
  const insight = priceInsight.value
  if (!insight) return []
  return [
    { key: 'median', label: t('results.card.priceShort.listing'), value: fmtPrice(insight.market_median_price) },
    { key: 'sold', label: t('results.card.priceShort.sold'), value: fmtPrice(soldReference.value?.median_price) },
    { key: 'low', label: t('results.card.priceShort.low'), value: fmtPrice(insight.min_price) },
  ].filter((entry) => entry.value !== '—')
})
</script>

<template>
  <Card class="group flex flex-col border-none shadow-glass hover:shadow-card-hover transition-all duration-300 rounded-2xl overflow-hidden bg-white/80 backdrop-blur-sm" :class="{ 'opacity-50': isHidden }">
    <!-- Image Header -->
    <div class="relative aspect-[2/1] overflow-hidden">
      <div class="absolute inset-0 bg-slate-200 animate-pulse" v-if="!imageUrl"></div>
      <img
        v-else
        :src="imageUrl"
        :alt="info.商品标题"
        class="w-full h-full object-cover transition-transform duration-500 group-hover:scale-110"
        loading="lazy"
      />
      <!-- Hidden overlay -->
      <div v-if="isHidden" class="absolute inset-0 bg-black/30 flex items-center justify-center">
        <span class="text-white/80 text-xs font-semibold uppercase tracking-wider">{{ hiddenLabel }}</span>
      </div>
      <!-- Overlays -->
      <div class="absolute top-3 left-3 flex gap-2">
        <Badge v-if="isRecommended && !isHidden" variant="default" class="bg-emerald-500/90 backdrop-blur-md border-none shadow-sm">
          {{ t('results.card.curated') }}
        </Badge>
        <Badge v-if="isRuleHidden" variant="secondary" class="bg-slate-900/75 text-white border-none backdrop-blur-md shadow-sm">
          {{ t('results.card.blacklisted') }}
        </Badge>
        <Badge v-if="reviewBadge" variant="secondary" class="border-none backdrop-blur-md shadow-sm" :class="reviewBadge.cls">
          {{ reviewBadge.label }}
        </Badge>
      </div>
      <div class="absolute top-3 right-3 flex gap-1.5">
        <button
          v-if="canToggleBlock"
          type="button"
          @click="emit('toggle-block', props.item)"
          :aria-label="isHidden ? t('results.card.unblock') : t('results.card.block')"
          class="flex rounded-full bg-white/30 p-1.5 text-white backdrop-blur-md border border-white/40 shadow-sm opacity-100 transition-opacity sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100 hover:bg-white/50"
        >
          <EyeOff v-if="!isHidden" class="w-4 h-4" />
          <Eye v-else class="w-4 h-4" />
        </button>
         <a
           :href="info.商品链接"
           target="_blank"
           rel="noopener noreferrer"
           :aria-label="t('results.card.detail')"
           class="flex rounded-full bg-white/30 p-1.5 text-white backdrop-blur-md border border-white/40 shadow-sm opacity-100 transition-opacity sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100"
         >
            <ExternalLink class="w-4 h-4" />
         </a>
      </div>
    </div>

    <CardHeader class="px-3 pt-2.5 pb-1.5">
      <div class="flex justify-between items-start gap-3">
        <CardTitle class="text-base font-semibold text-slate-800 line-clamp-2 leading-snug flex-grow h-10">
          <a :href="info.商品链接" target="_blank" rel="noopener noreferrer" class="hover:text-primary transition-colors">
            {{ info.商品标题 }}
          </a>
        </CardTitle>
      </div>
      <div class="flex items-baseline gap-1 mt-1.5">
        <span class="text-2xl font-bold text-rose-600 tracking-tight">{{ info.当前售价 }}</span>
        <span v-if="originalPrice" class="text-xs text-slate-400 line-through mb-1">{{ originalPrice }}</span>
      </div>
      <div v-if="hasListingFacts" class="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-slate-500">
        <Badge v-for="tag in listingTags" :key="tag" variant="secondary" class="px-1.5 py-0 text-[10px] font-medium bg-rose-50 text-rose-600 border border-rose-100">{{ tag }}</Badge>
        <span v-for="fact in facts" :key="fact.key">{{ fact.text }}</span>
      </div>
    </CardHeader>

    <CardContent class="px-3 pt-1.5 pb-2.5 flex-grow">
      <!-- AI Insight Section -->
      <div class="rounded-xl p-2.5 border border-slate-100" :class="recommendationStatus.bg">
        <div class="flex items-center justify-between" :class="criteriaChips.length || showReason ? 'mb-1.5' : ''">
          <div class="flex items-center gap-2">
            <component :is="recommendationStatus.icon" class="w-4 h-4" :class="recommendationStatus.text" />
            <span class="text-sm font-bold" :class="recommendationStatus.text">{{ recommendationStatus.label }}</span>
          </div>
          <div v-if="!isSkipped && criteriaChips.length === 0" class="flex items-center gap-1">
             <span class="text-[10px] font-medium uppercase tracking-wider text-slate-400">{{ t('results.card.aiMatch') }}</span>
             <span class="text-sm font-black" :class="recommendationStatus.text">{{ matchScore }}%</span>
          </div>
        </div>

        <!-- 逐项核对：有 criteria_analysis 就用标签代替百分比，一眼看到哪条不符；紧凑模式下通过项折起来 -->
        <div v-if="criteriaChips.length" :class="showReason ? 'mb-2' : ''">
          <div class="flex flex-wrap gap-1">
            <button
              v-for="chip in visibleChips"
              :key="chip.key"
              type="button"
              class="inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] font-medium leading-tight transition-colors"
              :class="[CHIP_CLASS[chip.state], activeChipKey === chip.key ? 'ring-2 ring-offset-1 ring-slate-300' : '']"
              :title="chip.comment || chip.label"
              :aria-pressed="activeChipKey === chip.key"
              @click="toggleChip(chip.key)"
            >
              <component :is="CHIP_ICON[chip.state]" class="w-3 h-3 shrink-0" />
              <span>{{ chip.label }}</span>
              <span v-if="chip.short" class="font-bold">{{ chip.short }}</span>
            </button>
            <button
              v-if="hiddenPassCount > 0"
              type="button"
              class="inline-flex items-center gap-1 rounded-full border border-dashed border-emerald-300 bg-emerald-50/50 px-1.5 py-0.5 text-[10px] font-medium leading-tight text-emerald-700 transition-colors hover:bg-emerald-50"
              :aria-expanded="false"
              @click="passExpanded = true"
            >
              <CheckCircle2 class="w-3 h-3 shrink-0" />
              {{ visibleChips.length ? t('results.card.passSummary', { count: hiddenPassCount }) : t('results.card.allPass', { count: hiddenPassCount }) }}
            </button>
            <button
              v-else-if="passExpanded && !detailsOpen && passCount > 0"
              type="button"
              class="inline-flex items-center gap-0.5 rounded-full px-1.5 py-0.5 text-[10px] font-medium leading-tight text-slate-400 transition-colors hover:text-slate-600"
              :aria-expanded="true"
              @click="passExpanded = false"
            >
              {{ t('results.card.collapsePass') }}
              <ChevronDown class="w-3 h-3 rotate-180" />
            </button>
          </div>
          <div v-if="activeChip" class="mt-1.5 rounded-lg bg-white/70 px-2 py-1.5 text-[11px] leading-relaxed text-slate-600">
            <span class="font-bold">{{ activeChip.label }} · {{ t(`results.criteria.state.${activeChip.state}`) }}</span>
            <span v-if="activeChip.comment"> {{ activeChip.comment }}</span>
            <p v-if="activeChip.evidence" class="mt-0.5 text-[10px] text-slate-400">{{ t('results.criteria.evidence', { evidence: activeChip.evidence }) }}</p>
          </div>
        </div>
        <div v-else-if="!isSkipped" class="w-full h-1.5 bg-white/50 rounded-full overflow-hidden mb-2">
          <div
            class="h-full transition-all duration-1000 ease-out rounded-full"
            :class="recommendationStatus.color"
            :style="{ width: `${matchScore}%` }"
          ></div>
        </div>

        <template v-if="showReason">
          <p class="text-xs leading-relaxed text-slate-600" :class="{ 'line-clamp-2': !expanded }">
             {{ ai?.reason || t('results.card.analyzing') }}
          </p>
          <button
            type="button"
            v-if="ai?.reason && ai.reason.length > 50"
            @click="expanded = !expanded"
            class="mt-1 text-[10px] font-bold uppercase text-primary/70 hover:text-primary transition-colors flex items-center gap-1"
          >
            {{ expanded ? t('results.card.collapse') : t('results.card.expand') }}
            <Info class="w-3 h-3" />
          </button>
        </template>
      </div>

      <!-- Price Stats：紧凑模式两行（贵贱 + 三个参考价），详情模式贵贱行 + 三个格子 -->
      <div v-if="priceInsight?.observation_count" class="mt-2">
        <div
          v-if="!detailsOpen"
          class="rounded-xl border px-2.5 py-1.5 text-[11px] leading-snug"
          :class="priceVerdict ? priceVerdict.style : 'border-dashed border-slate-200 text-slate-500'"
        >
          <div class="flex flex-wrap items-center gap-x-1.5">
            <template v-if="priceVerdict">
              <span class="font-bold">{{ priceVerdict.label }}</span>
              <span>{{ priceVerdict.detail }}</span>
            </template>
            <span v-else class="text-slate-400">{{ t('results.card.verdict.insufficient') }}</span>
          </div>
          <div v-if="priceLine.length" class="mt-0.5 flex flex-wrap items-center gap-x-2 text-slate-500">
            <span v-for="entry in priceLine" :key="entry.key" class="whitespace-nowrap">
              <span class="text-slate-400">{{ entry.label }}</span> <span class="font-semibold text-slate-700">{{ entry.value }}</span>
            </span>
          </div>
        </div>
        <div v-else class="space-y-2">
          <div v-if="priceVerdict" class="flex flex-wrap items-center gap-x-2 gap-y-0.5 rounded-xl border px-3 py-1.5 text-[11px]" :class="priceVerdict.style">
            <span class="font-bold">{{ priceVerdict.label }}</span>
            <span>{{ priceVerdict.detail }}</span>
          </div>
          <div v-else class="rounded-xl border border-dashed border-slate-200 px-3 py-1.5 text-[11px] text-slate-400">
            {{ t('results.card.verdict.insufficient') }}
          </div>
          <div class="grid grid-cols-3 gap-2">
            <div class="bg-slate-50/50 p-2 rounded-xl border border-slate-100/50">
              <div class="flex items-center gap-1 text-[10px] font-medium text-slate-400 mb-1">
                <TrendingUp class="w-3 h-3" /> {{ t('results.card.listingMedian') }}
              </div>
              <div class="text-sm font-bold text-slate-700">{{ fmtPrice(priceInsight.market_median_price) }}</div>
              <div class="text-[10px] text-slate-400">{{ t('results.card.samples', { count: priceInsight.market_sample_count ?? 0 }) }}</div>
            </div>
            <div class="bg-slate-50/50 p-2 rounded-xl border border-slate-100/50">
              <div class="flex items-center gap-1 text-[10px] font-medium text-slate-400 mb-1">
                <Tag class="w-3 h-3" /> {{ t('results.card.soldReference') }}
              </div>
              <div class="text-sm font-bold" :class="soldReference?.reliable ? 'text-slate-700' : 'text-slate-400'">{{ fmtPrice(soldReference?.median_price) }}</div>
              <div class="text-[10px] text-slate-400">
                {{ soldReference?.sample_count ? t('results.card.samples', { count: soldReference.sample_count }) : t('results.card.noSamples') }}
              </div>
            </div>
            <div class="bg-slate-50/50 p-2 rounded-xl border border-slate-100/50">
              <div class="flex items-center gap-1 text-[10px] font-medium text-slate-400 mb-1">
                <TrendingDown class="w-3 h-3" /> {{ t('results.card.historicalLow') }}
              </div>
              <div class="text-sm font-bold text-slate-700">{{ fmtPrice(priceInsight.min_price) }}</div>
              <div class="text-[10px] text-slate-400">{{ t('results.card.observations', { count: priceInsight.observation_count }) }}</div>
            </div>
          </div>
        </div>
      </div>

      <!-- Seller Profile：紧凑模式三行（标题 + 最多 3 个信号），展开看结论和明细 -->
      <div v-if="profile" class="mt-2 rounded-xl border border-slate-100 bg-slate-50/60 p-2.5">
        <button
          type="button"
          @click="profileOverride = !profileOpen"
          class="flex w-full items-center justify-between gap-2 text-left"
          :aria-expanded="profileOpen"
        >
          <span class="flex min-w-0 flex-1 items-center gap-1.5 text-[10px] font-bold uppercase tracking-wider text-slate-400">
            <User class="w-3 h-3 shrink-0" />
            <span class="shrink-0">{{ t('results.card.sellerProfile') }}</span>
            <span v-if="profile.摘要" class="min-w-0 normal-case tracking-normal font-medium text-slate-500" :class="profileOpen ? '' : 'truncate'">· {{ profile.摘要 }}</span>
          </span>
          <ChevronDown class="w-3 h-3 shrink-0 text-slate-400 transition-transform" :class="{ 'rotate-180': profileOpen }" />
        </button>
        <div v-if="visibleSignals.length" class="mt-1.5 flex flex-wrap gap-1">
          <span
            v-for="signal in visibleSignals"
            :key="signal"
            class="rounded-full border px-1.5 py-0.5 text-[10px]"
            :class="isWarningSignal(signal) ? 'border-amber-200 bg-amber-50 text-amber-700' : 'border-emerald-200 bg-emerald-50 text-emerald-700'"
          >{{ signal }}</span>
          <button
            v-if="hiddenSignalCount > 0"
            type="button"
            class="rounded-full border border-dashed border-slate-300 px-1.5 py-0.5 text-[10px] text-slate-500 transition-colors hover:bg-white"
            :aria-expanded="false"
            @click="profileOverride = true"
          >+{{ hiddenSignalCount }}</button>
        </div>
        <p v-if="profileOpen && profile.结论" class="mt-1.5 text-xs leading-relaxed text-slate-600">{{ profile.结论 }}</p>
        <dl v-if="profileOpen && profileRows.length" class="mt-2 space-y-1 text-[11px] text-slate-500">
          <div v-for="row in profileRows" :key="row.key" class="flex gap-2">
            <dt class="w-8 shrink-0 font-medium text-slate-400">{{ row.label }}</dt>
            <dd class="leading-relaxed">{{ row.value }}</dd>
          </div>
        </dl>
      </div>

      <!-- 收件箱操作 -->
      <div class="mt-2.5 flex flex-wrap gap-1">
        <button
          v-for="action in reviewActions"
          :key="action.status"
          type="button"
          class="inline-flex items-center gap-1 rounded-full border px-2 py-1 text-[11px] font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          :class="action.cls"
          @click="emit('review', props.item, action.status)"
        >
          <component :is="action.icon" class="w-3 h-3" />
          {{ action.label }}
        </button>
      </div>
    </CardContent>

    <!-- 页脚：抓取时间 | 展开/收起 · 详情链接；昵称已经在卖家画像标题里，没有画像时才放这 -->
    <CardFooter class="px-3 py-2 bg-slate-50/30 border-t border-slate-100/60 flex items-center justify-between gap-2 text-[10px]">
      <div class="flex min-w-0 items-center gap-2 text-slate-400">
        <div v-if="!profile" class="flex min-w-0 items-center gap-1">
          <User class="w-3 h-3 shrink-0" />
          <span class="truncate max-w-[60px]">{{ seller.卖家昵称 || info.卖家昵称 || t('results.card.anonymous') }}</span>
        </div>
        <div class="flex shrink-0 items-center gap-1">
          <Clock class="w-3 h-3" />
          <span>{{ t('results.card.crawledAt', { time: crawlTime }) }}</span>
        </div>
      </div>
      <div class="flex shrink-0 items-center gap-2.5">
        <button
          type="button"
          class="inline-flex items-center gap-0.5 font-medium text-slate-400 transition-colors hover:text-slate-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded"
          :aria-expanded="detailsOpen"
          @click="toggleDetails"
        >
          {{ detailsOpen ? t('results.card.hideDetails') : t('results.card.showDetails') }}
          <ChevronDown class="w-3 h-3 transition-transform" :class="{ 'rotate-180': detailsOpen }" />
        </button>
        <a :href="info.商品链接" target="_blank" rel="noopener noreferrer" class="flex items-center gap-1 text-primary font-bold hover:gap-1.5 transition-all">
          {{ t('results.card.detail') }} <ExternalLink class="w-3 h-3" />
        </a>
      </div>
    </CardFooter>
  </Card>
</template>
