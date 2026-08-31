import MiniSearch from 'minisearch'
import {
  TOPIC_IDS,
  type FilterState,
  type PublicDataset,
  type RadarEvent,
  type TopicId,
} from '../types'

export const DEFAULT_FILTERS: FilterState = {
  q: '',
  range: 'all',
  topic: 'all',
  company: 'all',
  eventType: 'all',
  evidence: 'all',
  verification: 'all',
  minScore: 0,
  status: 'all',
}

const FORBIDDEN_PUBLIC_KEYS = new Set([
  'recipient',
  'recipient_email',
  'email',
  'raw_html',
  'raw_content',
  'prompt',
  'system_prompt',
  'delivery',
  'deliveries',
  'delivery_state',
  'agentmail_draft_id',
  'message_id',
  'api_key',
  'secret',
])

export function assertPublicDataset(value: unknown): asserts value is PublicDataset {
  if (!value || typeof value !== 'object') {
    throw new Error('公开数据格式无效')
  }

  const dataset = value as Record<string, unknown>
  if (dataset.public_export !== true || !Array.isArray(dataset.events)) {
    throw new Error('数据未标记为公开脱敏导出')
  }

  const walk = (node: unknown, path: string): void => {
    if (!node || typeof node !== 'object') return
    if (Array.isArray(node)) {
      node.forEach((item, index) => walk(item, `${path}[${index}]`))
      return
    }
    for (const [key, child] of Object.entries(node)) {
      if (FORBIDDEN_PUBLIC_KEYS.has(key.toLowerCase())) {
        throw new Error(`公开数据包含禁用字段：${path}.${key}`)
      }
      walk(child, `${path}.${key}`)
    }
  }
  walk(dataset, 'dataset')

  const assertHttpUrl = (input: unknown, path: string): void => {
    if (input === undefined || input === null) return
    if (typeof input !== 'string') throw new Error(`公开链接格式无效：${path}`)
    let parsed: URL
    try {
      parsed = new URL(input)
    } catch {
      throw new Error(`公开链接格式无效：${path}`)
    }
    if (!['http:', 'https:'].includes(parsed.protocol)) {
      throw new Error(`公开链接协议不安全：${path}`)
    }
  }
  ;(dataset.events as Array<Record<string, unknown>>).forEach((event, index) => {
    assertHttpUrl(event.primary_url, `events[${index}].primary_url`)
    const corroborating = Array.isArray(event.corroborating_urls)
      ? event.corroborating_urls
      : []
    corroborating.forEach((link, linkIndex) =>
      assertHttpUrl(
        (link as Record<string, unknown>)?.url,
        `events[${index}].corroborating_urls[${linkIndex}].url`,
      ),
    )
    const paperLinks = event.paper_links as Record<string, unknown> | null | undefined
    if (paperLinks) {
      Object.entries(paperLinks).forEach(([key, url]) =>
        assertHttpUrl(url, `events[${index}].paper_links.${key}`),
      )
    }
  })
}

export function normalizePublicDataset(value: PublicDataset): PublicDataset {
  const generatedAt = value.generated_at || new Date(0).toISOString()
  return {
    ...value,
    schema_version: value.schema_version || '1.0',
    timezone: value.timezone || 'Asia/Shanghai',
    source_health: value.source_health ?? {
      healthy: 0,
      degraded: 0,
      last_success_at: null,
      notices: [],
    },
    events: value.events.map((input, index) => {
      const event = input as Partial<RadarEvent>
      const sourceTime = event.source_time || event.published_at || event.first_seen_at || generatedAt
      return {
        event_id: event.event_id || `public-event-${index}`,
        cluster_id: event.cluster_id || event.event_id || `public-cluster-${index}`,
        event_type: event.event_type || 'RESEARCH_REPORT',
        topics: event.topics?.filter((topic) => TOPIC_IDS.includes(topic)) ?? [],
        entities: event.entities ?? [],
        title_zh: event.title_zh || '未命名公开事件',
        summary_zh: event.summary_zh || '暂无公开摘要。',
        why_it_matters: event.why_it_matters || '等待进一步验证其影响。',
        change_summary: event.change_summary || '首次收录，暂无上一版本可比较。',
        source_time: sourceTime,
        published_at: event.published_at || sourceTime,
        first_seen_at: event.first_seen_at || sourceTime,
        material_updated_at: event.material_updated_at,
        status: event.status || 'NEW_ENTITY',
        source_type: event.source_type || 'public_source',
        verification_status: event.verification_status || 'company_claim',
        evidence_type: event.evidence_type || 'official_company',
        score: Number.isFinite(event.score) ? Number(event.score) : 0,
        primary_url: event.primary_url || '#',
        corroborating_urls: event.corroborating_urls ?? [],
        paper_links: event.paper_links,
        deep_read: event.deep_read,
        tags: event.tags ?? [],
        timeline: event.timeline ?? [],
      }
    }),
  }
}

export function createSearchIndex(events: RadarEvent[]) {
  const segmenter = new Intl.Segmenter('zh-CN', { granularity: 'word' })
  const index = new MiniSearch<RadarEvent>({
    idField: 'event_id',
    fields: ['title_zh', 'summary_zh', 'why_it_matters', 'entity_names', 'tag_text'],
    storeFields: ['event_id'],
    tokenize: (text) =>
      [...segmenter.segment(text)]
        .filter((segment) => segment.isWordLike)
        .map((segment) => segment.segment.toLocaleLowerCase('zh-CN')),
    processTerm: (term) => term.toLocaleLowerCase('zh-CN'),
    searchOptions: {
      boost: { title_zh: 3, entity_names: 2, tag_text: 1.5 },
      fuzzy: 0.18,
      prefix: true,
    },
  })
  index.addAll(
    events.map((event) => ({
      ...event,
      entity_names: event.entities.map((entity) => entity.name).join(' '),
      tag_text: event.tags.join(' '),
    })),
  )
  return index
}

export function filterEvents(
  events: RadarEvent[],
  filters: FilterState,
  generatedAt: string,
  searchIds?: Set<string>,
) {
  const now = new Date(generatedAt).getTime()
  const ranges = { '24h': 24, '7d': 24 * 7, '30d': 24 * 30 } as const
  const cutoff = filters.range === 'all' ? null : now - ranges[filters.range] * 60 * 60 * 1000
  const activityAt = (event: RadarEvent) =>
    Date.parse(event.material_updated_at || event.published_at || event.first_seen_at || event.source_time)

  return events
    .filter((event) => !searchIds || searchIds.has(event.event_id))
    .filter((event) => !cutoff || activityAt(event) >= cutoff)
    .filter((event) => filters.topic === 'all' || event.topics.includes(filters.topic))
    .filter(
      (event) =>
        filters.company === 'all' || event.entities.some((entity) => entity.id === filters.company),
    )
    .filter((event) => filters.eventType === 'all' || event.event_type === filters.eventType)
    .filter((event) => filters.evidence === 'all' || event.evidence_type === filters.evidence)
    .filter(
      (event) =>
        filters.verification === 'all' || event.verification_status === filters.verification,
    )
    .filter((event) => filters.status === 'all' || event.status === filters.status)
    .filter((event) => event.score >= filters.minScore)
    .sort((a, b) => activityAt(b) - activityAt(a) || b.score - a.score)
}

export function filtersFromSearch(search: string): FilterState {
  const params = new URLSearchParams(search)
  const range = params.get('range')
  const topic = params.get('topic')
  const minScore = Number(params.get('score'))
  const validRanges = ['all', '24h', '7d', '30d']

  return {
    q: params.get('q') ?? '',
    range: validRanges.includes(range ?? '') ? (range as FilterState['range']) : 'all',
    topic: TOPIC_IDS.includes(topic as TopicId) ? (topic as TopicId) : 'all',
    company: params.get('company') ?? 'all',
    eventType: params.get('type') ?? 'all',
    evidence: (params.get('evidence') as FilterState['evidence']) ?? 'all',
    verification: (params.get('verify') as FilterState['verification']) ?? 'all',
    minScore: Number.isFinite(minScore) && minScore >= 0 && minScore <= 100 ? minScore : 0,
    status: (params.get('status') as FilterState['status']) ?? 'all',
  }
}

export function filtersToSearch(filters: FilterState) {
  const params = new URLSearchParams()
  const entries: [string, string | number][] = [
    ['q', filters.q],
    ['range', filters.range],
    ['topic', filters.topic],
    ['company', filters.company],
    ['type', filters.eventType],
    ['evidence', filters.evidence],
    ['verify', filters.verification],
    ['score', filters.minScore],
    ['status', filters.status],
  ]
  entries.forEach(([key, value]) => {
    const defaultValue = {
      q: '',
      range: 'all',
      topic: 'all',
      company: 'all',
      type: 'all',
      evidence: 'all',
      verify: 'all',
      score: 0,
      status: 'all',
    }[key]
    if (String(value) !== String(defaultValue)) params.set(key, String(value))
  })
  const query = params.toString()
  return query ? `?${query}` : ''
}

export function scoreBand(score: number) {
  if (score >= 80) return 'alert'
  if (score >= 65) return 'focus'
  if (score >= 45) return 'standard'
  return 'archive'
}
