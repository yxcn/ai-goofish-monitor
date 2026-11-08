// Based on the data structure from web_server.py and scraper.py

export interface ProductInfo {
  "商品标题": string;
  "当前售价": string;
  "商品原价"?: string;
  "“想要”人数"?: string | number;
  "商品标签"?: string[];
  "发货地区"?: string;
  "卖家昵称"?: string;
  "商品链接": string;
  "发布时间"?: string;
  "商品ID": string;
  "商品图片列表"?: string[];
  "商品主图链接"?: string;
  "浏览量"?: string | number;
  "收藏数"?: string | number;
  // 以下来自详情接口的 sellerDO，比搜索结果里的省份细
  "卖家所在城市"?: string;
  "卖家最近活跃"?: string;
  "卖家24小时回复率"?: string;
  "卖家平均回复时长"?: string;
}

// 程序按固定规则从卖家资料算出的摘要（src/seller_profile.py）
export interface SellerProfile {
  "摘要"?: string;
  "身份"?: string;
  "活跃"?: string;
  "交易"?: string;
  "历史"?: string;
  "口碑"?: string;
  "结论"?: string;
  "商家判断"?: string;
  "商家迹象"?: string[];
  "信号"?: string[];
  "品类分布"?: Record<string, number>;
  "指标"?: Record<string, number | string | null>;
}

export interface SellerInfo {
  "卖家昵称"?: string;
  "卖家头像链接"?: string;
  "卖家个性签名"?: string;
  "卖家在售/已售商品数"?: string;
  "卖家收到的评价总数"?: string;
  "卖家信用等级"?: string;
  "买家信用等级"?: string;
  "卖家芝麻信用"?: string;
  "卖家注册时长"?: string;
  "作为卖家的好评数"?: string;
  "作为卖家的好评率"?: string;
  "作为买家的好评数"?: string;
  "作为买家的好评率"?: string;
  "卖家发布的商品列表"?: any[]; // Define more strictly if needed
  "卖家收到的评价列表"?: any[]; // Define more strictly if needed
}

// AI 逐项核对结果：status 为 PASS / FAIL / NEEDS_MANUAL_CHECK，short 是 ≤8 字的事实结论（新版提示词才有）
export interface CriteriaEntry {
  status?: string;
  short?: string;
  comment?: string;
  evidence?: string;
  persona?: string;
  [key: string]: unknown;
}

// AI 用量：本商品所有请求的合计（含重试）。网关没返回 usage 时 token 字段为 null；没配单价时 cost_usd 为 null
export interface AiUsage {
  input_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  calls: number;
  elapsed_ms: number;
  cost_usd: number | null;
}

export interface AiAnalysis {
  is_recommended: boolean;
  reason: string;
  // 'attribute_rule': blocked by attribute pre-filter; 'skipped': SKIP_AI_ANALYSIS pushed without AI review
  analysis_source?: 'ai' | 'keyword' | 'attribute_rule' | 'skipped';
  keyword_hit_count?: number;
  value_score?: number;
  value_summary?: string;
  prompt_version?: string;
  // 哪家、哪个模型给的结论（2026-11 加入系统设置切换提供方之后才有）
  ai_provider?: 'openai' | 'claude';
  ai_model?: string;
  // 本商品的耗时 / token / 成本（2026-11 加入）；更早的记录没有
  ai_usage?: AiUsage;
  risk_tags?: string[];
  criteria_analysis?: Record<string, CriteriaEntry>;
  matched_keywords?: string[];
  error?: string;
}

// 成交参考价：近 30 天消失的挂牌 + 卖家已售同类（见 price_history_service.build_sold_reference）
export interface SoldReference {
  sample_count: number;
  avg_price: number | null;
  median_price: number | null;
  min_price: number | null;
  max_price: number | null;
  vanished_count: number;
  sold_listing_count: number;
  window_days: number;
  reliable: boolean;
}

export interface PriceInsight {
  observation_count: number;
  current_price?: number | null;
  avg_price?: number | null;
  median_price?: number | null;
  min_price?: number | null;
  max_price?: number | null;
  market_avg_price?: number | null;
  market_median_price?: number | null;
  market_sample_count?: number;
  sold_reference?: SoldReference;
  vs_listing_median_percent?: number | null;
  vs_sold_reference_percent?: number | null;
  price_verdict?: string;
  price_verdict_key?: 'good_deal' | 'fair' | 'pricey' | 'insufficient';
  price_verdict_basis?: 'sold' | 'listing' | null;
  reference_price?: number | null;
  reference_sample_count?: number;
  price_diff_percent?: number | null;
  price_verdict_text?: string;
  price_change_amount?: number | null;
  price_change_percent?: number | null;
  deal_score?: number | null;
  deal_label?: string;
  first_seen_at?: string | null;
  last_seen_at?: string | null;
}

export interface ResultInsights {
  market_summary: {
    sample_count: number;
    avg_price: number | null;
    median_price: number | null;
    min_price: number | null;
    max_price: number | null;
    snapshot_time?: string | null;
  };
  history_summary: {
    unique_items: number;
    sample_count: number;
    avg_price: number | null;
    median_price: number | null;
    min_price: number | null;
    max_price: number | null;
  };
  daily_trend: Array<{
    day: string;
    sample_count: number;
    avg_price: number | null;
    median_price: number | null;
    min_price: number | null;
    max_price: number | null;
  }>;
  sold_reference?: SoldReference;
  latest_snapshot_at?: string | null;
}

export interface ResultItem {
  "爬取时间": string;
  "搜索关键字": string;
  "任务名称": string;
  "商品信息": ProductInfo;
  "卖家信息": SellerInfo;
  "卖家画像"?: SellerProfile;
  ai_analysis: AiAnalysis;
  price_insight?: PriceInsight;
  _status?: 'active' | 'hidden' | 'expired';
  _effective_hidden?: boolean;
  _hidden_reason?: 'manual' | 'rule' | 'expired' | null;
  _matched_blacklist_keywords?: string[];
  // 用户在收件箱里标的处理状态（result_items.review_status），没标过就是 new
  _review_status?: ReviewStatus;
  _reviewed_at?: string | null;
}

export type ReviewStatus = 'new' | 'contacted' | 'excluded' | 'bought'

// GET /api/results/{file}/review-summary：只统计可见记录
export interface ReviewSummary {
  inbox: number;
  not_recommended: number;
  contacted: number;
  excluded: number;
  bought: number;
  total: number;
}
