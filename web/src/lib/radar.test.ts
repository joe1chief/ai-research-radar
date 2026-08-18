import { describe, expect, it } from 'vitest'
import type { PublicDataset } from '../types'
import { publicDatasetFixture } from '../test/fixtures/public-dataset'
import {
  DEFAULT_FILTERS,
  assertPublicDataset,
  createSearchIndex,
  filterEvents,
  filtersFromSearch,
  filtersToSearch,
  normalizePublicDataset,
} from './radar'

const dataset = publicDatasetFixture

describe('公开数据行为', () => {
  it('固定测试夹具满足公开数据约束', () => {
    expect(() => assertPublicDataset(dataset)).not.toThrow()
    expect(dataset.events).toHaveLength(2)
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

describe('空公开数据集', () => {
  const emptyDataset: PublicDataset = {
    ...dataset,
    demo_mode: false,
    facets: {},
    events: [],
  }

  it('可以 normalize 且保持空事件集', () => {
    const normalized = normalizePublicDataset(emptyDataset)

    expect(normalized.events).toEqual([])
    expect(() => assertPublicDataset(normalized)).not.toThrow()
  })

  it('可以创建搜索索引并返回空结果', () => {
    const index = createSearchIndex(emptyDataset.events)

    expect(index.search('智谱')).toEqual([])
  })

  it('可以执行筛选并返回空结果', () => {
    expect(
      filterEvents(emptyDataset.events, DEFAULT_FILTERS, emptyDataset.generated_at),
    ).toEqual([])
  })
})
