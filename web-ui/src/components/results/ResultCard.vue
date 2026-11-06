<script setup lang="ts">
import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ResultItem } from '@/types/result.d.ts'
import {
  Card,
  CardContent,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import Badge from '@/components/ui/badge/Badge.vue'
import { ExternalLink, TrendingUp, TrendingDown, Info, User, Clock, CheckCircle2, XCircle, AlertCircle, EyeOff, Eye, ChevronDown, Tag } from 'lucide-vue-next'
import { formatDateTime } from '@/i18n'

interface Props {
  item: ResultItem
}

const props = defineProps<Props>()
const emit = defineEmits<{
  (e: 'toggle-block', item: ResultItem): void
}>()
const { t } = useI18n()

const info = props.item.商品信息
const seller = props.item.卖家信息
const ai = props.item.ai_analysis
const priceInsight = props.item.price_insight

const isRecommended = ai?.is_recommended === true
// 和通知消息同一套规则：reason 以“有条件推荐”开头的推荐单独标注
const isConditional = computed(() => typeof ai?.reason === 'string' && ai.reason.trimStart().startsWith('有条件推荐'))
const recommendationStatus = computed(() => {
  if (ai?.is_recommended === true && isConditional.value) return { label: t('results.card.conditionalRecommend'), color: 'bg-amber-500', icon: CheckCircle2, text: 'text-amber-700', bg: 'bg-amber-50' }
  if (ai?.is_recommended === true) return { label: t('results.card.strongRecommend'), color: 'bg-emerald-500', icon: CheckCircle2, text: 'text-emerald-600', bg: 'bg-emerald-50' }
  if (ai?.is_recommended === false) return { label: t('results.card.notRecommended'), color: 'bg-rose-500', icon: XCircle, text: 'text-rose-600', bg: 'bg-rose-50' }
  return { label: t('results.card.pending'), color: 'bg-amber-500', icon: AlertCircle, text: 'text-amber-600', bg: 'bg-amber-50' }
})

const imageUrl = info.商品图片列表?.[0] || info.商品主图链接 || ''
const crawlTime = props.item.爬取时间
  ? formatDateTime(props.item.爬取时间, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
  : t('common.unknown')
const matchScore = ai?.value_score ?? 0
// 商品页上的那几项：标签（包邮/验货宝）、想要、浏览、收藏、发货地、发布时间；占位符（NaN、-）不显示
const listingTags = computed(() => Array.from(new Set((info.商品标签 ?? []).map((tag) => String(tag ?? '').trim()).filter(Boolean))))
const toCount = (value: unknown): number | null => {
  if (value === null || value === undefined || value === '') return null
  const parsed = Number(String(value).trim())
  return Number.isFinite(parsed) ? Math.round(parsed) : null
}
const wantsCount = computed(() => toCount(info['“想要”人数']))
const viewCount = computed(() => toCount(info.浏览量))
const favoriteCount = computed(() => toCount(info.收藏数))
const publishTime = computed(() => {
  const raw = String(info.发布时间 ?? '').trim()
  if (!raw || raw === '未知时间') return ''
  return raw.replace(/^\d{4}-/, '')
})
// 发货地区是省份，详情页还给了卖家城市；两者都有且不重复时合成“山东 淄博”
const region = computed(() => {
  const rawProvince = String(info.发货地区 ?? '').trim()
  const province = rawProvince && rawProvince !== '地区未知' ? rawProvince : ''
  const city = String(info.卖家所在城市 ?? '').trim()
  if (province && city && !province.includes(city) && !city.includes(province)) return `${province} ${city}`
  return city || province
})
// 卖家活跃度：最近活跃（“21分钟前来过”）、24 小时回复率、平均回复时长
const sellerFacts = computed(() => {
  const parts: string[] = []
  const lastActive = String(info.卖家最近活跃 ?? '').trim()
  const replyRate = String(info.卖家24小时回复率 ?? '').trim()
  const replyInterval = String(info.卖家平均回复时长 ?? '').trim()
  if (lastActive) parts.push(lastActive)
  if (replyRate) parts.push(t('results.card.replyRate', { rate: replyRate }))
  if (replyInterval) parts.push(t('results.card.replyIn', { time: replyInterval }))
  return parts
})
// 划线原价：占位符（暂无）或和现价相同时不显示
const originalPrice = computed(() => {
  const raw = String(info.商品原价 ?? '').trim()
  if (!raw || ['暂无', '-', 'N/A'].includes(raw)) return ''
  const digits = (text: string) => text.replace(/[^\d.]/g, '')
  return digits(raw) && digits(raw) === digits(String(info.当前售价 ?? '')) ? '' : raw
})
const hasListingFacts = computed(() => listingTags.value.length > 0 || wantsCount.value !== null || viewCount.value !== null || favoriteCount.value !== null || !!publishTime.value || !!region.value)
const isHidden = computed(() => props.item._effective_hidden === true || props.item._status === 'hidden')
const isRuleHidden = computed(() => props.item._hidden_reason === 'rule')
const canToggleBlock = computed(() => props.item._hidden_reason !== 'rule' && props.item._hidden_reason !== 'expired')
const hiddenLabel = computed(() => {
  if (props.item._hidden_reason === 'rule') return t('results.card.blacklisted')
  if (props.item._hidden_reason === 'expired') return t('results.card.expired')
  return t('results.card.hidden')
})

const expanded = ref(false)

// 卖家画像：程序按固定规则算出的摘要；结论和信号常驻，其余几行展开看
const profile = props.item.卖家画像 && typeof props.item.卖家画像 === 'object' ? props.item.卖家画像 : null
const profileExpanded = ref(false)
const profileSignals = computed(() => (profile?.信号 ?? []).map((s) => String(s ?? '').trim()).filter(Boolean))
const profileRows = computed(() => {
  if (!profile) return []
  const rows: Array<{ key: string; label: string; value: string }> = [
    { key: 'identity', label: t('results.card.profileIdentity'), value: String(profile.身份 ?? '') },
    { key: 'activity', label: t('results.card.profileActivity'), value: String(profile.活跃 ?? '') },
    { key: 'trades', label: t('results.card.profileTrades'), value: String(profile.交易 ?? '') },
    { key: 'history', label: t('results.card.profileHistory'), value: String(profile.历史 ?? '') },
    { key: 'reputation', label: t('results.card.profileReputation'), value: String(profile.口碑 ?? '') },
  ]
  return rows.filter((row) => row.value && row.value !== '暂无')
})
const isWarningSignal = (signal: string) => signal.startsWith('⚠️')

// 贵贱判断：后端先比成交参考价，样本不足再比挂牌中位价；都不够就不下结论
const fmtPrice = (value: number | null | undefined) => (value === null || value === undefined ? '—' : `¥${Math.round(value)}`)
const priceVerdict = computed(() => {
  const key = priceInsight?.price_verdict_key
  if (!key || key === 'insufficient') return null
  const basis = priceInsight?.price_verdict_basis === 'sold' ? t('results.card.basisSold') : t('results.card.basisListing')
  const pct = priceInsight?.price_diff_percent ?? 0
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
const soldReference = computed(() => priceInsight?.sold_reference ?? null)
</script>

<template>
  <Card class="group flex flex-col h-full border-none shadow-glass hover:shadow-card-hover transition-all duration-300 rounded-2xl overflow-hidden bg-white/80 backdrop-blur-sm" :class="{ 'opacity-50': isHidden }">
    <!-- Image Header -->
    <div class="relative aspect-[4/3] overflow-hidden">
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

    <CardHeader class="p-4 pb-2">
      <div class="flex justify-between items-start gap-3">
        <CardTitle class="text-base font-semibold text-slate-800 line-clamp-2 leading-snug flex-grow h-10">
          <a :href="info.商品链接" target="_blank" rel="noopener noreferrer" class="hover:text-primary transition-colors">
            {{ info.商品标题 }}
          </a>
        </CardTitle>
      </div>
      <div class="flex items-baseline gap-1 mt-2">
        <span class="text-2xl font-bold text-rose-600 tracking-tight">{{ info.当前售价 }}</span>
        <span v-if="originalPrice" class="text-xs text-slate-400 line-through mb-1">{{ originalPrice }}</span>
      </div>
      <div v-if="hasListingFacts" class="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-slate-500">
        <Badge v-for="tag in listingTags" :key="tag" variant="secondary" class="px-1.5 py-0 text-[10px] font-medium bg-rose-50 text-rose-600 border border-rose-100">{{ tag }}</Badge>
        <span v-if="wantsCount !== null">{{ t('results.card.wants', { count: wantsCount }) }}</span>
        <span v-if="viewCount !== null">{{ t('results.card.views', { count: viewCount }) }}</span>
        <span v-if="favoriteCount !== null">{{ t('results.card.favorites', { count: favoriteCount }) }}</span>
        <span v-if="region">{{ region }}</span>
        <span v-if="publishTime">{{ t('results.card.published', { time: publishTime }) }}</span>
      </div>
      <div v-if="sellerFacts.length" class="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-slate-500">
        <User class="w-3 h-3 text-slate-400" />
        <span v-for="fact in sellerFacts" :key="fact">{{ fact }}</span>
      </div>
    </CardHeader>

    <CardContent class="p-4 pt-2 flex-grow">
      <!-- AI Insight Section -->
      <div class="rounded-xl p-3 border border-slate-100" :class="recommendationStatus.bg">
        <div class="flex items-center justify-between mb-2">
          <div class="flex items-center gap-2">
            <component :is="recommendationStatus.icon" class="w-4 h-4" :class="recommendationStatus.text" />
            <span class="text-sm font-bold" :class="recommendationStatus.text">{{ recommendationStatus.label }}</span>
          </div>
          <div class="flex items-center gap-1">
             <span class="text-[10px] font-medium uppercase tracking-wider text-slate-400">AI Match</span>
             <span class="text-sm font-black" :class="recommendationStatus.text">{{ matchScore }}%</span>
          </div>
        </div>
        
        <div class="w-full h-1.5 bg-white/50 rounded-full overflow-hidden mb-3">
          <div 
            class="h-full transition-all duration-1000 ease-out rounded-full" 
            :class="recommendationStatus.color"
            :style="{ width: `${matchScore}%` }"
          ></div>
        </div>

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
      </div>

      <!-- Seller Profile -->
      <div v-if="profile" class="mt-3 rounded-xl border border-slate-100 bg-slate-50/60 p-3">
        <button
          type="button"
          @click="profileExpanded = !profileExpanded"
          class="flex w-full items-center justify-between text-left"
          :aria-expanded="profileExpanded"
        >
          <span class="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wider text-slate-400">
            <User class="w-3 h-3" /> {{ t('results.card.sellerProfile') }}
            <span v-if="profile.摘要" class="normal-case tracking-normal font-medium text-slate-500">· {{ profile.摘要 }}</span>
          </span>
          <ChevronDown class="w-3 h-3 shrink-0 text-slate-400 transition-transform" :class="{ 'rotate-180': profileExpanded }" />
        </button>
        <p v-if="profile.结论" class="mt-1.5 text-xs leading-relaxed text-slate-600">{{ profile.结论 }}</p>
        <div v-if="profileSignals.length" class="mt-1.5 flex flex-wrap gap-1">
          <span
            v-for="signal in profileSignals"
            :key="signal"
            class="rounded-full border px-1.5 py-0.5 text-[10px]"
            :class="isWarningSignal(signal) ? 'border-amber-200 bg-amber-50 text-amber-700' : 'border-emerald-200 bg-emerald-50 text-emerald-700'"
          >{{ signal }}</span>
        </div>
        <dl v-if="profileExpanded && profileRows.length" class="mt-2 space-y-1 text-[11px] text-slate-500">
          <div v-for="row in profileRows" :key="row.key" class="flex gap-2">
            <dt class="w-8 shrink-0 font-medium text-slate-400">{{ row.label }}</dt>
            <dd class="leading-relaxed">{{ row.value }}</dd>
          </div>
        </dl>
      </div>

      <!-- Price Stats -->
      <div v-if="priceInsight?.observation_count" class="mt-4 space-y-2">
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
    </CardContent>

    <CardFooter class="px-4 py-3 bg-slate-50/30 border-t border-slate-100/60 flex items-center justify-between text-[10px]">
      <div class="flex items-center gap-3 text-slate-400">
        <div class="flex items-center gap-1">
          <User class="w-3 h-3" />
          <span class="truncate max-w-[60px]">{{ seller.卖家昵称 || info.卖家昵称 || t('results.card.anonymous') }}</span>
        </div>
        <div class="flex items-center gap-1">
          <Clock class="w-3 h-3" />
          <span>{{ crawlTime }}</span>
        </div>
      </div>
      <a :href="info.商品链接" target="_blank" rel="noopener noreferrer" class="flex items-center gap-1 text-primary font-bold hover:gap-1.5 transition-all">
        {{ t('results.card.detail') }} <ExternalLink class="w-3 h-3" />
      </a>
    </CardFooter>
  </Card>
</template>
