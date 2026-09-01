export const TOPIC_IDS = [
  'long_horizon',
  'autonomous_agent',
  'self_evolving',
  'mechanistic_interpretability',
  'safety_governance',
  'industrial_capital',
  'podcast_culture',
] as const

export type TopicId = (typeof TOPIC_IDS)[number]

export type EventStatus =
  | 'NEW_ENTITY'
  | 'MATERIAL_UPDATE'
  | 'MINOR_UPDATE'
  | 'DISCOVERED_LATE'

export type VerificationStatus =
  | 'verified_primary'
  | 'corroborated'
  | 'company_claim'
  | 'reported_unconfirmed'

export type EvidenceType =
  | 'paper'
  | 'official_filing'
  | 'official_company'
  | 'open_source_release'
  | 'reputable_media'

export interface Entity {
  id: string
  name: string
  kind: 'company' | 'lab' | 'project' | 'issuer' | 'author_group'
}

export interface SupportingLink {
  label: string
  url: string
}

export interface PaperLinks {
  arxiv?: string
  alphaxiv?: string
  code?: string
  project?: string
}

export interface DeepRead {
  summary: string
  key_findings: string[]
}

export interface TimelineEntry {
  at: string
  label: string
  detail: string
  kind: 'source' | 'discovery' | 'update'
}

export interface RadarEvent {
  event_id: string
  cluster_id: string
  event_type: string
  topics: TopicId[]
  entities: Entity[]
  title_zh: string
  summary_zh: string
  why_it_matters: string
  change_summary: string
  source_time: string
  published_at: string
  first_seen_at: string
  material_updated_at?: string | null
  status: EventStatus
  source_type: string
  verification_status: VerificationStatus
  evidence_type: EvidenceType
  score: number
  primary_url: string
  corroborating_urls: SupportingLink[]
  paper_links?: PaperLinks | null
  deep_read?: DeepRead | null
  key_quotes?: string[]
  deep_takeaway?: string
  related_events?: Array<{
    event_id: string
    title_zh: string
    published_at: string
    score: number
  }>
  tags: string[]
  timeline: TimelineEntry[]
}

export interface SourceHealth {
  healthy: number
  degraded: number
  last_success_at: string | null
  notices: string[]
}

export interface PublicFacets {
  topics?: Record<string, number>
  entities?: Record<string, number>
  event_types?: Record<string, number>
  evidence_types?: Record<string, number>
  verification_statuses?: Record<string, number>
  statuses?: Record<string, number>
}

export interface PublicDataset {
  schema_version: string
  public_export: true
  demo_mode?: boolean
  generated_at: string
  timezone: string
  source_health: SourceHealth
  facets?: PublicFacets
  events: RadarEvent[]
}

export interface MonthIndex {
  schema_version: string
  generated_at: string
  months: Array<{ month: string; count: number }>
}

export interface FilterState {
  q: string
  range: 'all' | '24h' | '7d' | '30d'
  topic: TopicId | 'all'
  company: string
  eventType: string
  evidence: EvidenceType | 'all'
  verification: VerificationStatus | 'all'
  minScore: number
  status: EventStatus | 'all'
}
