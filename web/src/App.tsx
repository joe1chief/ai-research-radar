import { useEffect, useMemo, useState } from 'react'
import type {
  EvidenceType,
  EventStatus,
  FilterState,
  MonthIndex,
  PublicDataset,
  RadarEvent,
  TopicId,
  VerificationStatus,
} from './types'
import {
  DEFAULT_FILTERS,
  assertPublicDataset,
  createSearchIndex,
  filterEvents,
  filtersFromSearch,
  filtersToSearch,
  normalizePublicDataset,
  scoreBand,
} from './lib/radar'

const TOPICS: Record<
  TopicId,
  { index: string; eyebrow: string; title: string; short: string; description: string }
> = {
  long_horizon: {
    index: '01',
    eyebrow: 'LONG HORIZON',
    title: '长程任务',
    short: '长程任务',
    description: '长期规划、持久记忆与跨小时至跨月的自主执行。',
  },
  autonomous_agent: {
    index: '02',
    eyebrow: 'AUTONOMOUS SYSTEMS',
    title: '自治智能体系统',
    short: '自治智能体',
    description: '工具使用、多智能体协作、数字员工与 Agent Infra。',
  },
  self_evolving: {
    index: '03',
    eyebrow: 'SELF-EVOLVING',
    title: '完全自我训练',
    short: '自我进化',
    description: 'Self-Play、合成数据、RLVR 与递归自我改进。',
  },
  mechanistic_interpretability: {
    index: '04',
    eyebrow: 'MECHANISTIC INTERPRETABILITY',
    title: '机械可解释性',
    short: '机械可解释性',
    description: '定位模型内部表征、回路和因果机制。',
  },
  safety_governance: {
    index: '04',
    eyebrow: 'SAFETY & GOVERNANCE',
    title: '极致安全治理',
    short: '安全治理',
    description: '机械可解释性、AI Control、审计与可扩展监督。',
  },
  industrial_capital: {
    index: '05',
    eyebrow: 'INDUSTRIAL CAPITAL',
    title: '产业与资本',
    short: '产业资本',
    description: '融资、上市、并购、算力投入与监管级披露。',
  },
}

const SECTION_IDS: TopicId[] = [
  'long_horizon',
  'autonomous_agent',
  'self_evolving',
  'safety_governance',
  'industrial_capital',
]

function isInSection(event: RadarEvent, topicId: TopicId) {
  if (topicId === 'safety_governance') {
    return (
      event.topics.includes('safety_governance') ||
      event.topics.includes('mechanistic_interpretability')
    )
  }
  return event.topics.includes(topicId)
}

const EVIDENCE_LABELS: Record<EvidenceType, string> = {
  paper: '论文原文',
  official_filing: '监管披露',
  official_company: '公司官方',
  open_source_release: '开源发布',
  reputable_media: '可信媒体',
}

const VERIFICATION_LABELS: Record<VerificationStatus, string> = {
  verified_primary: '一级来源已核验',
  corroborated: '多源交叉验证',
  company_claim: '公司主张',
  reported_unconfirmed: '待官方确认',
}

const STATUS_LABELS: Record<EventStatus, string> = {
  NEW_ENTITY: '首次收录',
  MATERIAL_UPDATE: '重大更新',
  MINOR_UPDATE: '轻微更新',
  DISCOVERED_LATE: '延迟发现',
}

const EVENT_TYPE_LABELS: Record<string, string> = {
  PAPER: '论文',
  RESEARCH_REPORT: '研究报告',
  MODEL_RELEASE: '模型发布',
  OPEN_SOURCE_RELEASE: '开源发布',
  PROTOCOL_RELEASE: '协议发布',
  SAFETY_RESEARCH: '安全研究',
  IPO_FILING: '上市披露',
  RAISE: '融资',
  M_AND_A: '并购',
  CAPEX_COMPUTE: '算力投入',
  MATERIAL_CONTRACT: '重大合同',
  EARNINGS_GUIDANCE: '财报指引',
  OWNERSHIP: '股权变动',
  REGULATORY_EXPORT: '监管事件',
}

const DATE_FORMATTER = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

function formatDate(value: string) {
  return DATE_FORMATTER.format(new Date(value))
}

function setFilter<K extends keyof FilterState>(
  setter: React.Dispatch<React.SetStateAction<FilterState>>,
  key: K,
  value: FilterState[K],
) {
  setter((current) => ({ ...current, [key]: value }))
}

function DisruptScore({ value }: { value: number }) {
  const band = scoreBand(value)
  return (
    <div className={`tc-score-badge tc-score-badge--${band}`} aria-label={`破坏性分值 ${value} 分`}>
      <span>⚡ {value}</span>
      <span>SCORE</span>
    </div>
  )
}

function EvidencePill({ event }: { event: RadarEvent }) {
  return (
    <span className="tc-evidence-pill">
      <span aria-hidden="true" className="tc-evidence-dot" />
      {VERIFICATION_LABELS[event.verification_status]}
    </span>
  )
}

function EventLinks({ event }: { event: RadarEvent }) {
  const links = [
    { label: '查看一级来源', url: event.primary_url, primary: true },
    ...(event.paper_links?.arxiv
      ? [{ label: 'arXiv', url: event.paper_links.arxiv, primary: false }]
      : []),
    ...(event.paper_links?.alphaxiv
      ? [{ label: 'alphaXiv', url: event.paper_links.alphaxiv, primary: false }]
      : []),
    ...(event.paper_links?.code
      ? [{ label: '代码', url: event.paper_links.code, primary: false }]
      : []),
    ...(event.paper_links?.project
      ? [{ label: '项目页', url: event.paper_links.project, primary: false }]
      : []),
    ...event.corroborating_urls.map((item) => ({ ...item, primary: false })),
  ]

  return (
    <div className="tc-card__links" aria-label="事件来源链接">
      {links.map((link) => (
        <a
          className={link.primary ? 'tc-source-link tc-source-link--primary' : 'tc-source-link'}
          href={link.url}
          key={`${link.label}-${link.url}`}
          target="_blank"
          rel="noreferrer"
        >
          {link.label}
          <span aria-hidden="true">↗</span>
        </a>
      ))}
    </div>
  )
}

function LeadStoryCard({ event }: { event: RadarEvent }) {
  const mainTopic = TOPICS[event.topics[0]]
  const entityName = event.entities[0]?.name ?? 'Frontier Lab'
  return (
    <article className="lead-story-card">
      <div>
        <div className="lead-eyebrow">
          <span>{mainTopic.eyebrow} // LEAD STORY</span>
          <span>•</span>
          <span>{EVENT_TYPE_LABELS[event.event_type] ?? event.event_type}</span>
        </div>

        <h3 className="lead-title">{event.title_zh}</h3>
        <p className="lead-summary">{event.summary_zh}</p>

        {event.why_it_matters && (
          <div className="crunch-analysis-box">
            <span className="crunch-analysis-kicker">⚡ CRUNCH ANALYSIS // 核心研判价值</span>
            <p className="crunch-analysis-text">{event.why_it_matters}</p>
          </div>
        )}

        {event.deep_read?.summary && (
          <div className="tc-deep-read-box">
            <span className="insight-label">📖 alphaXiv 深度精读</span>
            <p>{event.deep_read.summary}</p>
          </div>
        )}
      </div>

      <div>
        <div className="lead-byline-bar">
          <div className="byline-meta-info">
            <span>By <strong className="byline-author">{entityName}</strong></span>
            <span>•</span>
            <span>{formatDate(event.published_at)}</span>
            <span>•</span>
            <EvidencePill event={event} />
          </div>
          <div className="disrupt-badge">
            <span>DISRUPT INDEX:</span>
            <span>{event.score} PTS</span>
          </div>
        </div>

        <div style={{ marginTop: '14px' }}>
          <EventLinks event={event} />
        </div>
      </div>
    </article>
  )
}

function HeadlineCardItem({ event, rank }: { event: RadarEvent; rank: string }) {
  const mainTopic = TOPICS[event.topics[0]]
  const entityName = event.entities[0]?.name ?? 'Lab'
  return (
    <div className="headline-card-item">
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '4px' }}>
        <span className="headline-card-item__rank">{rank} // {mainTopic.short}</span>
        <DisruptScore value={event.score} />
      </div>
      <h4 className="headline-card-item__title">{event.title_zh}</h4>
      <p className="headline-card-item__summary">{event.summary_zh}</p>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', fontSize: '11.5px', color: 'var(--tc-text-muted)' }}>
        <span>By {entityName} • {formatDate(event.published_at)}</span>
        <a
          className="tc-source-link tc-source-link--primary"
          style={{ padding: '4px 10px', fontSize: '11px' }}
          href={event.primary_url}
          target="_blank"
          rel="noreferrer"
        >
          查看 ↗
        </a>
      </div>
    </div>
  )
}

function HeroMiniCard({ event }: { event: RadarEvent }) {
  const mainTopic = TOPICS[event.topics[0]]
  const entityName = event.entities[0]?.name ?? 'Lab'
  return (
    <div className="hero-mini-card">
      <div>
        <div className="hero-mini-card__eyebrow">
          {mainTopic.eyebrow} · {EVENT_TYPE_LABELS[event.event_type] ?? event.event_type}
        </div>
        <h4 className="hero-mini-card__title">{event.title_zh}</h4>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', fontSize: '11px', color: 'var(--tc-text-muted)', paddingTop: '8px', borderTop: '1px solid var(--tc-border-light)' }}>
        <span>By {entityName}</span>
        <DisruptScore value={event.score} />
      </div>
    </div>
  )
}

function EventCard({ event, compact = false }: { event: RadarEvent; compact?: boolean }) {
  const mainTopic = TOPICS[event.topics[0]]
  const entityName = event.entities[0]?.name ?? 'Frontier Lab'
  return (
    <article className={`tc-card${compact ? ' tc-card--compact' : ''}`} id={event.event_id}>
      <header className="tc-card__header">
        <div className="tc-card__kicker-strip">
          <span className="tc-tag-kicker">{mainTopic.eyebrow}</span>
          <span>•</span>
          <span className="tc-entity-kicker">
            {EVENT_TYPE_LABELS[event.event_type] ?? event.event_type}
          </span>
          <span>•</span>
          <span className="tc-date-kicker">{formatDate(event.published_at)}</span>
        </div>
        <DisruptScore value={event.score} />
      </header>

      <h3 className="tc-card__title">{event.title_zh}</h3>
      <p className="tc-card__summary">{event.summary_zh}</p>

      {event.why_it_matters && (
        <div className="crunch-analysis-box">
          <span className="crunch-analysis-kicker">⚡ 核心研判 (Why it matters):</span>
          <p className="crunch-analysis-text">{event.why_it_matters}</p>
        </div>
      )}

      {!compact && event.deep_read?.summary && (
        <div className="tc-deep-read-box">
          <span className="insight-label">📖 alphaXiv 深度精读</span>
          <p>{event.deep_read.summary}</p>
        </div>
      )}

      <div className="tc-proof-strip">
        <span>By <strong style={{ color: 'var(--tc-black)' }}>{entityName}</strong></span>
        <span>•</span>
        <EvidencePill event={event} />
        <span>•</span>
        <span>{EVIDENCE_LABELS[event.evidence_type]}</span>
        <span>•</span>
        <span>{STATUS_LABELS[event.status]}</span>
        {event.material_updated_at && (
          <>
            <span>•</span>
            <span>实质更新 {formatDate(event.material_updated_at)}</span>
          </>
        )}
      </div>

      <div className="tc-tag-row" aria-label="标签">
        {event.entities.slice(0, 3).map((entity) => (
          <span className="tc-tag tc-tag--entity" key={entity.id}>
            @{entity.name}
          </span>
        ))}
        {event.tags.slice(0, compact ? 2 : 4).map((tag) => (
          <span className="tc-tag" key={tag}>
            #{tag}
          </span>
        ))}
      </div>

      <EventLinks event={event} />

      {!compact && event.timeline.length > 0 && (
        <details className="tc-timeline-accordion">
          <summary>查看事件演进脉络 · {event.timeline.length} 个节点</summary>
          <ul className="tc-timeline-list">
            {event.timeline.map((entry) => (
              <li className="tc-timeline-item" key={`${entry.at}-${entry.label}`}>
                <time className="tc-timeline-time">{formatDate(entry.at)}</time>
                <div className="tc-timeline-body">
                  <strong>{entry.label}</strong>
                  <p>{entry.detail}</p>
                </div>
              </li>
            ))}
          </ul>
        </details>
      )}
    </article>
  )
}

function LoadingState() {
  return (
    <main className="state-page" aria-live="polite">
      <div className="tc-spinner" aria-hidden="true" />
      <span
        style={{
          fontFamily: 'var(--font-tc-mono)',
          fontSize: '11px',
          fontWeight: 800,
          color: 'var(--tc-green-dark)',
          letterSpacing: '0.1em',
          textTransform: 'uppercase',
        }}
      >
        TECHCRUNCH // REALTIME SIGNAL ENGINE
      </span>
      <h1
        style={{
          fontFamily: 'var(--font-tc-headline)',
          fontSize: '26px',
          fontWeight: 900,
          marginTop: '8px',
          color: 'var(--tc-black)',
        }}
      >
        正在同步前沿 AI 研发与资本公开情报…
      </h1>
    </main>
  )
}

function App() {
  const [dataset, setDataset] = useState<PublicDataset | null>(null)
  const [latestDataset, setLatestDataset] = useState<PublicDataset | null>(null)
  const [months, setMonths] = useState<MonthIndex['months']>([])
  const [archiveMonth, setArchiveMonth] = useState(() => {
    const requested = new URLSearchParams(location.search).get('month') ?? ''
    return /^\d{4}-\d{2}$/.test(requested) ? requested : 'latest'
  })
  const [error, setError] = useState('')
  const [filters, setFilters] = useState<FilterState>(() => filtersFromSearch(location.search))
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    const dataUrl = `${import.meta.env.BASE_URL}data/latest.json`
    fetch(dataUrl, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        return response.json() as Promise<unknown>
      })
      .then((value) => {
        assertPublicDataset(value)
        const normalized = normalizePublicDataset(value)
        setLatestDataset(normalized)
        if (archiveMonth === 'latest') setDataset(normalized)
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === 'AbortError') return
        setError(reason instanceof Error ? reason.message : '未知错误')
      })
    fetch(`${import.meta.env.BASE_URL}data/months/index.json`, { signal: controller.signal })
      .then((response) =>
        response.ok ? response.json() : Promise.reject(new Error('month index')),
      )
      .then((value: MonthIndex) => {
        const valid = (value.months ?? []).filter((entry) => /^\d{4}-\d{2}$/.test(entry.month))
        setMonths(valid)
      })
      .catch(() => setMonths([]))
    return () => controller.abort()
  }, [])

  useEffect(() => {
    if (archiveMonth === 'latest') {
      if (latestDataset) setDataset(latestDataset)
      return
    }
    const controller = new AbortController()
    setError('')
    fetch(`${import.meta.env.BASE_URL}data/months/${archiveMonth}.json`, {
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        return response.json() as Promise<unknown>
      })
      .then((value) => {
        assertPublicDataset(value)
        setDataset(normalizePublicDataset(value))
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === 'AbortError') return
        setError(reason instanceof Error ? reason.message : '未知错误')
      })
    return () => controller.abort()
  }, [archiveMonth, latestDataset])

  useEffect(() => {
    const query = filtersToSearch(filters)
    const params = new URLSearchParams(query)
    if (archiveMonth !== 'latest') params.set('month', archiveMonth)
    const serialized = params.toString()
    history.replaceState(
      null,
      '',
      `${location.pathname}${serialized ? `?${serialized}` : ''}${location.hash}`,
    )
  }, [filters, archiveMonth])

  useEffect(() => {
    const onPopState = () => {
      setFilters(filtersFromSearch(location.search))
      const requested = new URLSearchParams(location.search).get('month') ?? ''
      setArchiveMonth(/^\d{4}-\d{2}$/.test(requested) ? requested : 'latest')
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  const searchIndex = useMemo(
    () => (dataset ? createSearchIndex(dataset.events) : null),
    [dataset],
  )

  const events = useMemo(() => {
    if (!dataset) return []
    const q = filters.q.trim()
    const ids =
      q && searchIndex
        ? new Set(searchIndex.search(q).map((result) => String(result.id)))
        : undefined
    return filterEvents(dataset.events, filters, dataset.generated_at, ids)
  }, [dataset, filters, searchIndex])

  const topLabs = useMemo(() => {
    if (!dataset) return []
    const map = new Map<string, { id: string; name: string; count: number }>()
    dataset.events.forEach((event) =>
      event.entities.forEach((entity) => {
        const prev = map.get(entity.id)
        if (prev) {
          prev.count += 1
        } else {
          map.set(entity.id, { id: entity.id, name: entity.name, count: 1 })
        }
      }),
    )
    return [...map.values()].sort((a, b) => b.count - a.count)
  }, [dataset])

  const share = async () => {
    try {
      await navigator.clipboard.writeText(location.href)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1800)
    } catch {
      setCopied(false)
    }
  }

  const resetAllFilters = () => {
    setFilters(DEFAULT_FILTERS)
    setArchiveMonth('latest')
  }

  if (error) {
    return (
      <main className="state-page">
        <span style={{ fontSize: '36px', color: 'var(--tc-alert-red)', marginBottom: '8px' }}>
          ⚠️
        </span>
        <span
          style={{
            fontFamily: 'var(--font-tc-mono)',
            fontSize: '11px',
            fontWeight: 800,
            color: 'var(--tc-alert-red)',
            textTransform: 'uppercase',
          }}
        >
          DATA DISRUPTED // 数据源读取异常
        </span>
        <h1
          style={{
            fontFamily: 'var(--font-tc-headline)',
            fontSize: '26px',
            fontWeight: 900,
            marginTop: '8px',
            color: 'var(--tc-black)',
          }}
        >
          公开科研情报归档暂时无法读取
        </h1>
        <p style={{ color: 'var(--tc-text-muted)', fontSize: '14px' }}>
          请确认 <code>data/latest.json</code> 已生成且通过脱敏检查。
        </p>
        <small style={{ fontFamily: 'var(--font-tc-mono)', color: 'var(--tc-text-subtle)' }}>
          {error}
        </small>
      </main>
    )
  }

  if (!dataset) return <LoadingState />

  const topThree = events.slice(0, 3)
  const heroBottomThree = events.slice(3, 6)
  const isDatasetEmpty = dataset.events.length === 0
  const isFiltered = filtersToSearch(filters) !== '' || archiveMonth !== 'latest'
  const visibleSectionIds: TopicId[] =
    filters.topic === 'all'
      ? SECTION_IDS
      : [filters.topic === 'mechanistic_interpretability' ? 'safety_governance' : filters.topic]

  return (
    <div className="app-shell">
      {/* ================= CRUNCH WIRE TICKER ================= */}
      <div className="crunch-wire">
        <div className="crunch-wire__left">
          <span className="crunch-wire__badge">
            <span className="pulse-dot" />
            CRUNCH WIRE
          </span>
          <span>上海时间 · {formatDate(dataset.generated_at)}</span>
          <span>•</span>
          <span style={{ color: 'var(--tc-green)' }}>29 信源全时校验</span>
          <span>•</span>
          <span>归档切片: {archiveMonth === 'latest' ? '最近 30 天' : archiveMonth}</span>
        </div>
        <div className="crunch-wire__right">
          <span>公开科研事件: {dataset.events.length} 条</span>
        </div>
      </div>

      {/* ================= TECHCRUNCH SITE HEADER ================= */}
      <header className="site-header">
        <div className="header-main">
          <a className="brand" href={import.meta.env.BASE_URL} aria-label="AI Research Radar 首页">
            <div className="brand__tc-box" aria-hidden="true">
              TC
            </div>
            <div className="brand__wordmark">
              <div className="brand__name">
                Tech<span>Radar</span>
              </div>
              <span className="brand__sub">Public AI Intelligence</span>
            </div>
          </a>

          <nav className="header-nav" aria-label="头部导航分类">
            <button
              type="button"
              className={`nav-link${filters.topic === 'all' ? ' nav-link--active' : ''}`}
              onClick={() => setFilter(setFilters, 'topic', 'all')}
            >
              Latest
            </button>
            {SECTION_IDS.map((topicId) => {
              const topic = TOPICS[topicId]
              const isActive =
                filters.topic === topicId ||
                (topicId === 'safety_governance' && filters.topic === 'mechanistic_interpretability')
              return (
                <button
                  key={topicId}
                  type="button"
                  className={`nav-link${isActive ? ' nav-link--active' : ''}`}
                  onClick={() => setFilter(setFilters, 'topic', topicId)}
                >
                  {topic.short}
                </button>
              )
            })}
          </nav>

          <div className="header__actions">
            <span className="live-health-pill">
              <span className="green-live-dot" />
              {dataset.source_health.healthy} 信源已核验
            </span>
            <button className="tc-button-primary" type="button" onClick={share}>
              {copied ? '✓ 链接已复制' : '分享雷达 ↗'}
            </button>
          </div>
        </div>

        {/* Secondary Category Strip */}
        <div className="category-strip">
          <div className="category-strip__inner">
            <button
              type="button"
              className={`cat-pill${filters.topic === 'all' ? ' cat-pill--active' : ''}`}
              onClick={() => setFilter(setFilters, 'topic', 'all')}
            >
              全部信号 <span className="cat-count">{dataset.events.length}</span>
            </button>
            {SECTION_IDS.map((topicId) => {
              const topic = TOPICS[topicId]
              const topicEventsCount = dataset.events.filter((event) =>
                isInSection(event, topicId),
              ).length
              const isActive =
                filters.topic === topicId ||
                (topicId === 'safety_governance' &&
                  filters.topic === 'mechanistic_interpretability')
              return (
                <button
                  key={topicId}
                  type="button"
                  className={`cat-pill${isActive ? ' cat-pill--active' : ''}`}
                  onClick={() => setFilter(setFilters, 'topic', topicId)}
                >
                  {topic.index} {topic.short}
                  <span className="cat-count">{topicEventsCount}</span>
                </button>
              )
            })}
          </div>
        </div>
      </header>

      {dataset.demo_mode && (
        <div className="demo-banner" role="note">
          <span
            style={{
              padding: '1px 6px',
              background: '#92400e',
              color: '#ffffff',
              borderRadius: '3px',
              fontFamily: 'var(--font-tc-mono)',
              fontSize: '10px',
              fontWeight: 800,
            }}
          >
            DEMO MODE
          </span>
          当前展示公开脱敏样例；生产导出将自动替换，且不包含内部 Prompt、原文或私有投递状态。
        </div>
      )}

      <main className="main-container">
        {/* ================= FEATURED HERO GRID (3-Story + Bottom Row) ================= */}
        <section className="featured-hero-section" aria-labelledby="featured-heading">
          {topThree.length > 0 ? (
            <div>
              <div className="hero-layout-grid">
                {/* Left Lead Story (Top 1) */}
                <LeadStoryCard event={topThree[0]} />

                {/* Right Top Headlines (Top 2 & 3) */}
                <div className="top-headlines-col">
                  <h3 className="headlines-header">
                    <span>TOP HEADLINES // 焦点速报</span>
                    <span style={{ fontSize: '11px', color: 'var(--tc-green-dark)', fontFamily: 'var(--font-tc-mono)' }}>
                      EDITORIAL TOP 3
                    </span>
                  </h3>
                  <div className="headline-cards-stack">
                    {topThree.slice(1, 3).map((event, idx) => (
                      <HeadlineCardItem event={event} key={event.event_id} rank={`0${idx + 2}`} />
                    ))}
                  </div>
                </div>
              </div>

              {/* Bottom 3-Card Featured Row */}
              {heroBottomThree.length > 0 && (
                <div className="hero-bottom-row">
                  {heroBottomThree.map((event) => (
                    <HeroMiniCard event={event} key={event.event_id} />
                  ))}
                </div>
              )}
            </div>
          ) : (
            <div className="empty-state">
              <h3>{isDatasetEmpty ? '等待首批公开信号' : '当前筛选条件下没有重点特稿'}</h3>
              <p>
                {isDatasetEmpty
                  ? '首次信源采集完成后将自动在此呈现。'
                  : '调整检索关键词或降低最低分数限制即可恢复展示。'}
              </p>
            </div>
          )}
        </section>

        {/* ================= TWO-COLUMN RIVER LAYOUT ================= */}
        <div className="river-layout">
          {/* Main Stream Feed Column (70%) */}
          <div className="main-stream-col">
            <div className="stream-section-header">
              <span className="stream-section-title">
                {isFiltered ? 'FILTERED STREAM // 筛选归档' : 'THE CRUNCH STREAM // 全景技术与资本脉络'}
              </span>
              <span className="stream-section-count">
                共计 <strong>{events.length}</strong> 条高价值前沿信号 · 按评级与时间排序
              </span>
            </div>

            {events.length === 0 ? (
              <div className="empty-state">
                <h3>这组检索条件下暂时没有收录信号</h3>
                <p>可以尝试清空搜索词、放宽日期范围或降低最低信号分。</p>
                {!isDatasetEmpty && (
                  <button
                    type="button"
                    className="tc-button-primary"
                    onClick={resetAllFilters}
                  >
                    重置全部筛选条件
                  </button>
                )}
              </div>
            ) : (
              visibleSectionIds.map((topicId) => {
                const topicEvents = events.filter((event) => isInSection(event, topicId))
                if (topicEvents.length === 0) return null
                const topic = TOPICS[topicId]
                return (
                  <section className={`topic-group-section topic-group-section--${topicId}`} key={topicId}>
                    <header className="topic-group-header">
                      <div className="topic-group-header__left">
                        <span className="topic-group-num">{topic.index}</span>
                        <div className="topic-group-titles">
                          <h2>
                            {topic.eyebrow} · {topic.title}
                          </h2>
                          <p>{topic.description}</p>
                        </div>
                      </div>
                      <span className="topic-count-pill">{topicEvents.length} 条信号</span>
                    </header>
                    <div className="stream-cards-list">
                      {topicEvents.map((event) => (
                        <EventCard event={event} key={event.event_id} />
                      ))}
                    </div>
                  </section>
                )
              })
            )}
          </div>

          {/* TechCrunch Sticky Sidebar Column (30%) */}
          <aside className="tc-sidebar-col" aria-label="TechCrunch 侧栏功能区">
            {/* Widget 1: Fast Filter & Signal Controls */}
            <div className="tc-side-widget">
              <h4 className="tc-side-widget__header">
                <span>SIGNAL CONTROL // 信号控制</span>
                <span style={{ fontSize: '10px', color: 'var(--tc-green-dark)' }}>ACTIVE</span>
              </h4>

              {/* Fast Search */}
              <label className="tc-side-search">
                <svg
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <circle cx="11" cy="11" r="8" />
                  <line x1="21" y1="21" x2="16.65" y2="16.65" />
                </svg>
                <input
                  type="search"
                  value={filters.q}
                  onChange={(event) => setFilter(setFilters, 'q', event.target.value)}
                  placeholder="搜索论文、技术标签..."
                />
              </label>

              {/* Fast Score Band Pills */}
              <div className="tc-side-pills">
                {[0, 45, 65, 80].map((score) => {
                  const isActive = filters.minScore === score
                  return (
                    <button
                      key={score}
                      type="button"
                      className={`tc-side-pill-btn${isActive ? ' tc-side-pill-btn--active' : ''}`}
                      onClick={() => setFilter(setFilters, 'minScore', score)}
                    >
                      {score === 0 ? '全部' : `${score}+ ${score >= 80 ? '⚡预警' : score >= 65 ? '重点' : '常规'}`}
                    </button>
                  )
                })}
              </div>

              {/* Dropdowns */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', marginTop: '12px' }}>
                <label style={{ display: 'flex', flexDirection: 'column', gap: '4px', fontSize: '11px', fontWeight: 700, color: 'var(--tc-text-muted)' }}>
                  <span>归档切片</span>
                  <select
                    style={{ padding: '6px 8px', borderRadius: '4px', border: '1px solid var(--tc-border-medium)', fontSize: '12px' }}
                    value={archiveMonth}
                    onChange={(event) => {
                      setArchiveMonth(event.target.value)
                      setFilter(setFilters, 'range', 'all')
                    }}
                  >
                    <option value="latest">最近 30 天</option>
                    {months.map((entry) => (
                      <option key={entry.month} value={entry.month}>
                        {entry.month} ({entry.count} 条)
                      </option>
                    ))}
                  </select>
                </label>

                <label style={{ display: 'flex', flexDirection: 'column', gap: '4px', fontSize: '11px', fontWeight: 700, color: 'var(--tc-text-muted)' }}>
                  <span>时间范围</span>
                  <select
                    style={{ padding: '6px 8px', borderRadius: '4px', border: '1px solid var(--tc-border-medium)', fontSize: '12px' }}
                    value={filters.range}
                    onChange={(event) => setFilter(setFilters, 'range', event.target.value as FilterState['range'])}
                  >
                    <option value="all">当前切片全部</option>
                    <option value="24h">最近 24 小时</option>
                    <option value="7d">最近 7 天</option>
                    <option value="30d">最近 30 天</option>
                  </select>
                </label>
              </div>

              {isFiltered && (
                <button
                  type="button"
                  onClick={resetAllFilters}
                  style={{
                    width: '100%',
                    marginTop: '14px',
                    padding: '6px',
                    fontSize: '11.5px',
                    fontWeight: 700,
                    color: 'var(--tc-alert-red)',
                    background: 'none',
                    border: '1px dashed var(--tc-alert-red)',
                    borderRadius: '4px',
                  }}
                >
                  清除所有筛选条件
                </button>
              )}
            </div>

            {/* Widget 2: Most Disruptive Top Signals Leaderboard */}
            <div className="tc-side-widget">
              <h4 className="tc-side-widget__header">
                <span>MOST DISRUPTIVE // 信号榜</span>
                <span style={{ fontSize: '10px', color: 'var(--tc-green-dark)' }}>TOP 5</span>
              </h4>
              <div className="tc-leaderboard-list">
                {events.slice(0, 5).map((event, idx) => (
                  <div className="tc-leaderboard-item" key={event.event_id}>
                    <div className="tc-leaderboard-num">0{idx + 1}</div>
                    <div className="tc-leaderboard-body">
                      <h5>
                        <a href={`#${event.event_id}`}>{event.title_zh}</a>
                      </h5>
                      <div className="tc-leaderboard-meta">
                        <span>⚡ {event.score} PTS</span> • <span>{event.entities[0]?.name ?? 'Lab'}</span>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* Widget 3: Lab & Venture Tracker */}
            <div className="tc-side-widget">
              <h4 className="tc-side-widget__header">
                <span>LAB & VENTURE // 前沿机构</span>
                <span style={{ fontSize: '10px', color: 'var(--tc-text-muted)' }}>{topLabs.length} 家</span>
              </h4>
              <div className="tc-chips-cloud">
                <button
                  type="button"
                  className={`tc-entity-chip${filters.company === 'all' ? ' tc-entity-chip--active' : ''}`}
                  onClick={() => setFilter(setFilters, 'company', 'all')}
                >
                  全部
                </button>
                {topLabs.map((lab) => {
                  const isActive = filters.company === lab.id
                  return (
                    <button
                      key={lab.id}
                      type="button"
                      className={`tc-entity-chip${isActive ? ' tc-entity-chip--active' : ''}`}
                      onClick={() => setFilter(setFilters, 'company', isActive ? 'all' : lab.id)}
                    >
                      @{lab.name} ({lab.count})
                    </button>
                  )
                })}
              </div>
            </div>

            {/* Widget 4: Newsletter / Intelligence Dispatch Box */}
            <div className="tc-newsletter-widget">
              <h4>Get the AI Research Radar Briefing</h4>
              <p>每天从一手论文、监管披露与开源协议中获取决定 AI 下一阶段的确定性信号。</p>
              <button
                type="button"
                className="tc-button-primary"
                style={{ width: '100%', justifyContent: 'center' }}
                onClick={share}
              >
                {copied ? '✓ 已复制雷达链接' : '分享此雷达订阅 ↗'}
              </button>
            </div>
          </aside>
        </div>
      </main>

      {/* ================= TECHCRUNCH DEEP FOOTER ================= */}
      <footer className="tc-footer">
        <div className="tc-footer-inner">
          <div className="tc-footer-brand">
            <div className="tc-footer-logo-row">
              <div className="brand__tc-box" style={{ width: '32px', height: '32px', fontSize: '18px' }} aria-hidden="true">
                TC
              </div>
              <h3>TechRadar // AI Research</h3>
            </div>
            <p>
              严苛像素级对齐 TechCrunch 视觉与排版体系，全天候追踪长程任务、自治智能体、自我进化、机械可解释性与产业资本公开情报。
            </p>
          </div>
          <div className="tc-footer-telemetry">
            <div className="tc-footer-telemetry-item">
              <span className="green-live-dot" />
              <span>{dataset.source_health.healthy} 个一级信源全时校验正常</span>
            </div>
            <div>
              {dataset.source_health.last_success_at
                ? `最新采集同步时间：${formatDate(dataset.source_health.last_success_at)}`
                : '尚无采集记录'}
            </div>
            <div>系统版本：TechCrunch Edition · Schema v{dataset.schema_version}</div>
          </div>
        </div>
        <div className="tc-footer-bottom">
          <span>TECHRADAR · TECHCRUNCH DESIGN SYSTEM ALIGNMENT</span>
          <span>公开信息归档，不构成投资建议 · 事实、公司主张与待确认报道分层展示</span>
        </div>
      </footer>
    </div>
  )
}

export default App
