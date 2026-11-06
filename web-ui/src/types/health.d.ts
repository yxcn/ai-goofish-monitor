// Mirrors the payload of GET /api/health/crawler.

export type CrawlerHealthLevel = 'ok' | 'warning' | 'critical' | 'unknown'
export type CrawlerHealthOverallLevel = Exclude<CrawlerHealthLevel, 'unknown'>

export type CrawlerHealthCategory =
  | 'ok'
  | 'login_expired'
  | 'risk_control'
  | 'api_drift'
  | 'zero_results'
  | 'config_error'
  | 'crash'
  | 'paused'
  | 'no_runs'

export interface CrawlerHealthCounters {
  search_items_total: number
  search_parse_failed: number
  detail_attempts: number
  detail_unavailable: number
  items_submitted: number
  /** The backend may add more counters without a frontend change. */
  [key: string]: number
}

export interface CrawlerTaskHealth {
  task_id: number
  task_name: string
  keyword: string
  level: CrawlerHealthLevel
  category: CrawlerHealthCategory
  /** Backend-rendered detail (Chinese); shown as is. */
  message: string
  last_run_at: string | null
  last_run_status: 'success' | 'failed' | null
  consecutive_failures: number
  paused_until: string | null
  counters: CrawlerHealthCounters
}

export interface CrawlerHealthSnapshot {
  overall_level: CrawlerHealthOverallLevel
  generated_at: string
  tasks: CrawlerTaskHealth[]
}
