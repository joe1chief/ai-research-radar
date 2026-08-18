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
    return event.topics.includes('safety_governance') || event.topics.includes('mechanistic_interpretability')
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

function Score({ value }: { value: number }) {
  const band = scoreBand(value)
  return (
    <div className={`score score--${band}`} aria-label={`信号分数 ${value} 分`}>
      <strong>{value}</strong>
      <span>signal</span>
    </div>
  )
}

function EvidencePill({ event }: { event: RadarEvent }) {
  return (
    <span className={`evidence evidence--${event.verification_status}`}>
      <span aria-hidden="true" className="evidence__dot" />
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
    <div className="card__links" aria-label="事件来源链接">
      {links.map((link) => (
        <a
          className={link.primary ? 'source-link source-link--primary' : 'source-link'}
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

function EventCard({ event, compact = false }: { event: RadarEvent; compact?: boolean }) {
  const mainTopic = TOPICS[event.topics[0]]
  return (
    <article className={`event-card${compact ? ' event-card--compact' : ''}`}>
      <div className="card__rail" aria-hidden="true" />
      <header className="card__header">
        <div className="card__meta">
          <span className="type-label">{EVENT_TYPE_LABELS[event.event_type] ?? event.event_type}</span>
          <span>{mainTopic.short}</span>
          <span>{formatDate(event.published_at)}</span>
        </div>
        <Score value={event.score} />
      </header>

      <h3>{event.title_zh}</h3>
      <p className="card__summary">{event.summary_zh}</p>

      {!compact && event.deep_read?.summary && (
        <div className="card__deep-read">
          <span className="insight-label">alphaXiv 深读</span>
          <p>{event.deep_read.summary}</p>
        </div>
      )}

      {!compact && (
        <div className="card__insight-grid">
          <div>
            <span className="insight-label">为什么归入该主题</span>
            <p>{event.topics.map((topic) => TOPICS[topic].short).join(' · ')}</p>
          </div>
          <div>
            <span className="insight-label">为什么重要</span>
            <p>{event.why_it_matters}</p>
          </div>
          <div>
            <span className="insight-label">与上次相比</span>
            <p>{event.change_summary}</p>
          </div>
        </div>
      )}

      <div className="card__proof">
        <EvidencePill event={event} />
        <span>{EVIDENCE_LABELS[event.evidence_type]}</span>
        <span>{STATUS_LABELS[event.status]}</span>
      </div>

      <div className="tag-row" aria-label="标签">
        {event.entities.slice(0, 2).map((entity) => (
          <span className="tag tag--entity" key={entity.id}>
            {entity.name}
          </span>
        ))}
        {event.tags.slice(0, compact ? 2 : 4).map((tag) => (
          <span className="tag" key={tag}>
            {tag}
          </span>
        ))}
      </div>

      {!compact && (
        <div className="card__timestamps">
          <span>事件时间 {formatDate(event.source_time)}</span>
          <span>首次发现 {formatDate(event.first_seen_at)}</span>
          {event.material_updated_at && <span>实质更新 {formatDate(event.material_updated_at)}</span>}
        </div>
      )}

      <EventLinks event={event} />

      {!compact && event.timeline.length > 0 && (
        <details className="timeline">
          <summary>查看事件时间线 · {event.timeline.length} 个节点</summary>
          <ol>
            {event.timeline.map((entry) => (
              <li key={`${entry.at}-${entry.label}`}>
                <span className={`timeline__marker timeline__marker--${entry.kind}`} />
                <time>{formatDate(entry.at)}</time>
                <div>
                  <strong>{entry.label}</strong>
                  <p>{entry.detail}</p>
                </div>
              </li>
            ))}
          </ol>
        </details>
      )}
    </article>
  )
}

function FilterPanel({
  filters,
  setFilters,
  dataset,
  months,
  archiveMonth,
  onArchiveMonth,
}: {
  filters: FilterState
  setFilters: React.Dispatch<React.SetStateAction<FilterState>>
  dataset: PublicDataset
  months: MonthIndex['months']
  archiveMonth: string
  onArchiveMonth: (month: string) => void
}) {
  const companies = useMemo(() => {
    const map = new Map<string, string>()
    dataset.events.forEach((event) =>
      event.entities.forEach((entity) => map.set(entity.id, entity.name)),
    )
    return [...map.entries()].sort((a, b) => a[1].localeCompare(b[1], 'zh-CN'))
  }, [dataset.events])
  const eventTypes = [...new Set(dataset.events.map((event) => event.event_type))].sort()

  return (
    <aside className="filter-panel" aria-label="筛选雷达事件">
      <div className="filter-panel__heading">
        <div>
          <span className="section-kicker">SIGNAL CONTROL</span>
          <h2>筛选信号</h2>
        </div>
        <button className="text-button" onClick={() => setFilters(DEFAULT_FILTERS)} type="button">
          清除
        </button>
      </div>

      <label className="search-field">
        <span>全文检索</span>
        <div>
          <span aria-hidden="true">⌕</span>
          <input
            type="search"
            value={filters.q}
            onChange={(event) => setFilter(setFilters, 'q', event.target.value)}
            placeholder="论文、公司、技术标签…"
          />
        </div>
      </label>

      <div className="filter-grid">
        <label>
          <span>归档切片</span>
          <select value={archiveMonth} onChange={(event) => onArchiveMonth(event.target.value)}>
            <option value="latest">最近 30 天</option>
            {months.map((entry) => (
              <option key={entry.month} value={entry.month}>
                {entry.month} · {entry.count} 条
              </option>
            ))}
          </select>
        </label>

        <label>
          <span>日期范围</span>
          <select
            value={filters.range}
            onChange={(event) =>
              setFilter(setFilters, 'range', event.target.value as FilterState['range'])
            }
          >
            <option value="all">当前切片全部</option>
            <option value="24h">最近 24 小时</option>
            <option value="7d">最近 7 天</option>
            <option value="30d">最近 30 天</option>
          </select>
        </label>

        <label>
          <span>主题</span>
          <select
            value={filters.topic}
            onChange={(event) =>
              setFilter(setFilters, 'topic', event.target.value as FilterState['topic'])
            }
          >
            <option value="all">全部主题</option>
            {Object.entries(TOPICS).map(([id, topic]) => (
              <option key={id} value={id}>
                {topic.short}
              </option>
            ))}
          </select>
        </label>

        <label>
          <span>公司 / 组织</span>
          <select
            value={filters.company}
            onChange={(event) => setFilter(setFilters, 'company', event.target.value)}
          >
            <option value="all">全部实体</option>
            {companies.map(([id, name]) => (
              <option key={id} value={id}>
                {name}
              </option>
            ))}
          </select>
        </label>

        <label>
          <span>事件类型</span>
          <select
            value={filters.eventType}
            onChange={(event) => setFilter(setFilters, 'eventType', event.target.value)}
          >
            <option value="all">全部类型</option>
            {eventTypes.map((type) => (
              <option key={type} value={type}>
                {EVENT_TYPE_LABELS[type] ?? type}
              </option>
            ))}
          </select>
        </label>

        <label>
          <span>证据</span>
          <select
            value={filters.evidence}
            onChange={(event) =>
              setFilter(setFilters, 'evidence', event.target.value as FilterState['evidence'])
            }
          >
            <option value="all">全部证据</option>
            {Object.entries(EVIDENCE_LABELS).map(([id, label]) => (
              <option key={id} value={id}>
                {label}
              </option>
            ))}
          </select>
        </label>

        <label>
          <span>验证等级</span>
          <select
            value={filters.verification}
            onChange={(event) =>
              setFilter(
                setFilters,
                'verification',
                event.target.value as FilterState['verification'],
              )
            }
          >
            <option value="all">全部等级</option>
            {Object.entries(VERIFICATION_LABELS).map(([id, label]) => (
              <option key={id} value={id}>
                {label}
              </option>
            ))}
          </select>
        </label>

        <label>
          <span>状态</span>
          <select
            value={filters.status}
            onChange={(event) =>
              setFilter(setFilters, 'status', event.target.value as FilterState['status'])
            }
          >
            <option value="all">全部状态</option>
            {Object.entries(STATUS_LABELS).map(([id, label]) => (
              <option key={id} value={id}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </div>

      <fieldset className="score-filter">
        <legend>最低信号分</legend>
        {[0, 45, 65, 80].map((score) => (
          <label key={score}>
            <input
              checked={filters.minScore === score}
              name="minimum-score"
              onChange={() => setFilter(setFilters, 'minScore', score)}
              type="radio"
            />
            <span>{score === 0 ? '全部' : `${score}+`}</span>
          </label>
        ))}
      </fieldset>

      <div className="legend">
        <span><i className="legend__dot legend__dot--alert" />80+ 预警</span>
        <span><i className="legend__dot legend__dot--focus" />65+ 重点</span>
        <span><i className="legend__dot legend__dot--standard" />45+ 常规</span>
      </div>
    </aside>
  )
}

function LoadingState() {
  return (
    <main className="state-page" aria-live="polite">
      <div className="radar-spinner" aria-hidden="true"><span /></div>
      <span className="section-kicker">SYNCHRONIZING</span>
      <h1>正在校准公开信号…</h1>
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
      .then((response) => (response.ok ? response.json() : Promise.reject(new Error('month index'))))
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
    const ids = q && searchIndex
      ? new Set(searchIndex.search(q).map((result) => String(result.id)))
      : undefined
    return filterEvents(dataset.events, filters, dataset.generated_at, ids)
  }, [dataset, filters, searchIndex])

  const share = async () => {
    try {
      await navigator.clipboard.writeText(location.href)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1800)
    } catch {
      setCopied(false)
    }
  }

  if (error) {
    return (
      <main className="state-page">
        <span className="state-icon" aria-hidden="true">!</span>
        <span className="section-kicker">DATA UNAVAILABLE</span>
        <h1>公开归档暂时无法读取</h1>
        <p>请确认 <code>data/latest.json</code> 已生成且通过脱敏检查。</p>
        <small>{error}</small>
      </main>
    )
  }
  if (!dataset) return <LoadingState />

  const topThree = events.slice(0, 3)
  const isFiltered = filtersToSearch(filters) !== '' || archiveMonth !== 'latest'
  const visibleSectionIds: TopicId[] = filters.topic === 'all'
    ? SECTION_IDS
    : [filters.topic === 'mechanistic_interpretability' ? 'safety_governance' : filters.topic]

  return (
    <div className="app-shell">
      <header className="site-header">
        <a className="brand" href={import.meta.env.BASE_URL} aria-label="AI Research Radar 首页">
          <span className="brand__mark" aria-hidden="true"><i /><i /><i /></span>
          <span>
            <strong>AI Research Radar</strong>
            <small>PUBLIC INTELLIGENCE ARCHIVE</small>
          </span>
        </a>
        <div className="header__actions">
          <span className="live-status">
            <i /> {dataset.source_health.last_success_at ? '数据已同步' : '等待首次成功采集'}
          </span>
          <button className="share-button" type="button" onClick={share}>
            {copied ? '链接已复制' : '分享当前视图'}
            <span aria-hidden="true">↗</span>
          </button>
        </div>
      </header>

      {dataset.demo_mode && (
        <div className="demo-banner" role="note">
          <span>示例模式</span>
          当前展示公开脱敏样例；生产导出会自动替换，且不会包含邮箱、原文、Prompt 或投递状态。
        </div>
      )}

      <main>
        <section className="hero">
          <div className="hero__copy">
            <span className="eyebrow"><i /> DAILY SIGNAL BRIEF · 上海时间</span>
            <h1>看清 AI 演进的<br /><em>有效信号。</em></h1>
            <p>从论文原文、公司发布与监管披露中，持续跟踪五条决定 AI 下一阶段的技术与资本脉络。</p>
          </div>
          <div className="hero__status">
            <span className="status-label">数据切片</span>
            <strong>{formatDate(dataset.generated_at)}</strong>
            <dl>
              <div><dt>公开事件</dt><dd>{dataset.events.length}</dd></div>
              <div><dt>健康信源</dt><dd>{dataset.source_health.healthy}</dd></div>
              <div><dt>降级信源</dt><dd>{dataset.source_health.degraded}</dd></div>
            </dl>
          </div>
        </section>

        <section className="top-signals" aria-labelledby="top-heading">
          <div className="section-heading section-heading--light">
            <div>
              <span className="section-kicker">EDITOR'S PRIORITY</span>
              <h2 id="top-heading">今日 Top 3</h2>
            </div>
            <p>按主题契合、证据、新颖性、影响和可行动性综合排序。</p>
          </div>
          {topThree.length > 0 ? (
            <div className="top-grid">
              {topThree.map((event, index) => (
                <div className="top-grid__item" key={event.event_id}>
                  <span className="top-rank">0{index + 1}</span>
                  <EventCard event={event} compact />
                </div>
              ))}
            </div>
          ) : (
            <div className="empty-state empty-state--dark">
              <span aria-hidden="true">◎</span>
              <h3>当前筛选下没有重点信号</h3>
              <p>调整左侧条件即可恢复今日优先级。</p>
            </div>
          )}
        </section>

        <div className="content-layout">
          <FilterPanel
            filters={filters}
            setFilters={setFilters}
            dataset={dataset}
            months={months}
            archiveMonth={archiveMonth}
            onArchiveMonth={(month) => {
              setArchiveMonth(month)
              setFilter(setFilters, 'range', 'all')
            }}
          />

          <div className="archive-content">
            <div className="archive-summary">
              <div>
                <span className="section-kicker">LIVE ARCHIVE</span>
                <h2>{isFiltered ? '筛选结果' : '全部情报脉络'}</h2>
              </div>
              <p><strong>{events.length}</strong> 条信号 · 按分数与发布时间排序</p>
            </div>

            {events.length === 0 ? (
              <div className="empty-state">
                <span aria-hidden="true">⌁</span>
                <h3>这组条件下暂时没有信号</h3>
                <p>可以降低最低分、扩大日期范围，或清除全文检索。</p>
                <button type="button" onClick={() => setFilters(DEFAULT_FILTERS)}>重置全部筛选</button>
              </div>
            ) : (
              visibleSectionIds.map((topicId) => {
                const topicEvents = events.filter((event) => isInSection(event, topicId))
                if (topicEvents.length === 0) return null
                const topic = TOPICS[topicId]
                return (
                  <section className={`topic-section topic-section--${topicId}`} key={topicId}>
                    <header className="topic-header">
                      <span className="topic-index">{topic.index}</span>
                      <div>
                        <span className="section-kicker">{topic.eyebrow}</span>
                        <h2>{topic.title}</h2>
                        <p>{topic.description}</p>
                      </div>
                      <span className="topic-count">{topicEvents.length} 条</span>
                    </header>
                    <div className="event-list">
                      {topicEvents.map((event) => <EventCard event={event} key={event.event_id} />)}
                    </div>
                  </section>
                )
              })
            )}
          </div>
        </div>
      </main>

      <footer className="site-footer">
        <div>
          <strong>AI Research Radar</strong>
          <p>公开信息归档，不构成投资建议。事实、公司主张与待确认报道始终分层展示。</p>
        </div>
        <div className="footer__health">
          <span><i /> {dataset.source_health.healthy} 个信源正常</span>
          <span>
            {dataset.source_health.last_success_at
              ? `最近成功同步 ${formatDate(dataset.source_health.last_success_at)}`
              : '尚无成功采集记录'}
          </span>
        </div>
      </footer>
    </div>
  )
}

export default App
