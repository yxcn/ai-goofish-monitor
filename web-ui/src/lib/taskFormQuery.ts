import type { LocationQuery } from 'vue-router'
import type { TaskGenerateRequest, TaskUpdate } from '@/types/task.d.ts'

export type TaskFormDefaults = Partial<TaskGenerateRequest & TaskUpdate>
type QueryValue = LocationQuery[string] | undefined

/** Query keys read by `parseTaskFormDefaults` (form field defaults). */
export const TASK_FORM_QUERY_KEYS = [
  'taskName',
  'keyword',
  'description',
  'cron',
  'account',
  'accountStrategy',
  'region',
  'newPublishOption',
  'minPrice',
  'maxPrice',
  'decisionMode',
  'keywordRules',
  'titleIncludeKeywords',
  'maxPages',
  'freeShipping',
  'personalOnly',
  'analyzeImages',
] as const

/** Query keys that open a task dialog, plus the form defaults they carry. */
export const TASK_DIALOG_QUERY_KEYS = ['create', 'edit', ...TASK_FORM_QUERY_KEYS] as const

/**
 * Return `query` without `keys`, or `null` when none of them is present.
 * Unrelated keys are kept as-is.
 */
export function withoutQueryKeys(query: LocationQuery, keys: Iterable<string>): LocationQuery | null {
  const strip = new Set<string>(keys)
  if (!Object.keys(query).some((key) => strip.has(key))) return null
  const rest: LocationQuery = {}
  for (const [key, value] of Object.entries(query)) {
    if (!strip.has(key)) rest[key] = value
  }
  return rest
}

/** Return `query` without any task-dialog key, or `null` when there is nothing to strip. */
export function withoutTaskDialogQuery(query: LocationQuery): LocationQuery | null {
  return withoutQueryKeys(query, TASK_DIALOG_QUERY_KEYS)
}

const TRUE_VALUES = new Set(['1', 'true', 'yes', 'on'])
const FALSE_VALUES = new Set(['0', 'false', 'no', 'off'])

function readString(value: QueryValue): string | undefined {
  if (typeof value !== 'string') return undefined
  const trimmed = value.trim()
  return trimmed.length > 0 ? trimmed : undefined
}

function readBoolean(value: QueryValue): boolean | undefined {
  const raw = readString(value)?.toLowerCase()
  if (!raw) return undefined
  if (TRUE_VALUES.has(raw)) return true
  if (FALSE_VALUES.has(raw)) return false
  return undefined
}

function readNumber(value: QueryValue): number | undefined {
  const raw = readString(value)
  if (!raw) return undefined
  const parsed = Number(raw)
  return Number.isFinite(parsed) ? parsed : undefined
}

function readKeywordRules(value: QueryValue): string[] | undefined {
  const raw = readString(value)
  if (!raw) return undefined
  const rules = raw
    .split(/[,\n]+/)
    .map((item) => item.trim())
    .filter((item) => item.length > 0)
  return rules.length > 0 ? rules : undefined
}

export function parseTaskFormDefaults(query: LocationQuery): TaskFormDefaults {
  const defaults: TaskFormDefaults = {}
  const taskName = readString(query.taskName)
  const keyword = readString(query.keyword)
  const description = readString(query.description)
  const cron = readString(query.cron)
  const accountStateFile = readString(query.account)
  const accountStrategy = readString(query.accountStrategy)
  const region = readString(query.region)
  const newPublishOption = readString(query.newPublishOption)
  const minPrice = readString(query.minPrice)
  const maxPrice = readString(query.maxPrice)
  const decisionMode = readString(query.decisionMode)
  const keywordRules = readKeywordRules(query.keywordRules)
  const titleIncludeKeywords = readKeywordRules(query.titleIncludeKeywords)
  const maxPages = readNumber(query.maxPages)
  const freeShipping = readBoolean(query.freeShipping)
  const personalOnly = readBoolean(query.personalOnly)
  const analyzeImages = readBoolean(query.analyzeImages)

  if (taskName) defaults.task_name = taskName
  if (keyword) defaults.keyword = keyword
  if (description !== undefined) defaults.description = description
  if (cron !== undefined) defaults.cron = cron
  if (accountStateFile) defaults.account_state_file = accountStateFile
  if (accountStrategy === 'auto' || accountStrategy === 'fixed' || accountStrategy === 'rotate') {
    defaults.account_strategy = accountStrategy
  }
  if (region !== undefined) defaults.region = region
  if (newPublishOption !== undefined) defaults.new_publish_option = newPublishOption
  if (minPrice !== undefined) defaults.min_price = minPrice
  if (maxPrice !== undefined) defaults.max_price = maxPrice
  if (decisionMode === 'ai' || decisionMode === 'keyword') defaults.decision_mode = decisionMode
  if (keywordRules) defaults.keyword_rules = keywordRules
  if (titleIncludeKeywords) defaults.title_include_keywords = titleIncludeKeywords
  if (maxPages !== undefined) defaults.max_pages = maxPages
  if (freeShipping !== undefined) defaults.free_shipping = freeShipping
  if (personalOnly !== undefined) defaults.personal_only = personalOnly
  if (analyzeImages !== undefined) defaults.analyze_images = analyzeImages

  return defaults
}
