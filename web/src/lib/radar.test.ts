import { describe, expect, it } from 'vitest'
import sample from '../../public/data/latest.json'
import type { PublicDataset } from '../types'
import {
  DEFAULT_FILTERS,
  assertPublicDataset,
  createSearchIndex,
  filterEvents,
  filtersFromSearch,
  filtersToSearch,
} from './radar'

const dataset = sample as PublicDataset

describe('公开归档数据', () => {
  it('样例数据满足脱敏约束', () => {
    expect(() => assertPublicDataset(dataset)).not.toThrow()
    expect(dataset.events.length).toBeGreaterThanOrEqual(5)
  })

  it('拒绝邮箱、原始正文或投递状态等私有字段', () => {
    expect(() =>
      assertPublicDataset({ ...dataset, recipient_email: 'private@example.com' }),
    ).toThrow(/禁用字段/)
    expect(() =>
      assertPublicDataset({
        ...dataset,
        events: [{ ...dataset.events[0], primary_url: 'javascript:alert(1)' }],
      }),
    ).toThrow(/协议不安全/)
  })

  it('可以组合主题、证据与最低分筛选', () => {
    const events = filterEvents(
      dataset.events,
      {
        ...DEFAULT_FILTERS,
        topic: 'industrial_capital',
        evidence: 'official_filing',
        minScore: 80,
      },
      dataset.generated_at,
    )
    expect(events).toHaveLength(1)
    expect(events[0].topics).toContain('industrial_capital')
  })

  it('MiniSearch 支持中文实体和英文标签检索', () => {
    const index = createSearchIndex(dataset.events)
    expect(index.search('智谱').length).toBeGreaterThan(0)
    expect(index.search('self play').length).toBeGreaterThan(0)
  })

  it('分享链接参数可往返恢复', () => {
    const state = {
      ...DEFAULT_FILTERS,
      q: 'agent memory',
      range: '7d' as const,
      topic: 'autonomous_agent' as const,
      minScore: 65,
    }
    expect(filtersFromSearch(filtersToSearch(state))).toEqual(state)
  })

  it('最近窗口按实质更新时间而不是旧发布时间筛选', () => {
    const updated = {
      ...dataset.events[0],
      published_at: '2025-01-01T00:00:00Z',
      first_seen_at: '2025-01-01T00:00:00Z',
      material_updated_at: dataset.generated_at,
      status: 'MATERIAL_UPDATE' as const,
    }
    const events = filterEvents(
      [updated],
      { ...DEFAULT_FILTERS, range: '24h' },
      dataset.generated_at,
    )
    expect(events).toHaveLength(1)
  })
})
