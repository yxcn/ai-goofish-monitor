import { http } from '@/lib/http'
import type { CrawlerHealthSnapshot } from '@/types/health.d.ts'

export async function getCrawlerHealth(): Promise<CrawlerHealthSnapshot> {
  return await http('/api/health/crawler')
}
