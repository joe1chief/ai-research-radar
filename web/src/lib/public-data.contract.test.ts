import { describe, expect, it } from 'vitest'
import publicData from '../../public/data/latest.json'
import monthIndex from '../../public/data/months/index.json'
import type { MonthIndex } from '../types'
import { assertPublicDataset } from './radar'

const typedMonthIndex = monthIndex as MonthIndex

describe('生产公开数据文件', () => {
  it('latest 满足数据契约、脱敏和安全链接约束，并允许空事件集', () => {
    expect(() => assertPublicDataset(publicData)).not.toThrow()

    assertPublicDataset(publicData)
    expect(publicData.schema_version).toBe('1.0')
    expect(Number.isNaN(Date.parse(publicData.generated_at))).toBe(false)
    expect(publicData.timezone).toBe('Asia/Shanghai')
    expect(publicData.source_health).toEqual(
      expect.objectContaining({
        healthy: expect.any(Number),
        degraded: expect.any(Number),
        notices: expect.any(Array),
      }),
    )
    expect(publicData.facets).toEqual(expect.any(Object))
    expect(publicData.events).toEqual(expect.any(Array))

    expect(() =>
      assertPublicDataset({ ...publicData, demo_mode: false, facets: {}, events: [] }),
    ).not.toThrow()
  })

  it('月份索引允许为空，并只包含合法月份与非负计数', () => {
    expect(typedMonthIndex.schema_version).toBe('1.0')
    expect(Number.isNaN(Date.parse(typedMonthIndex.generated_at))).toBe(false)
    expect(typedMonthIndex.months).toEqual(expect.any(Array))

    for (const entry of typedMonthIndex.months) {
      expect(entry.month).toMatch(/^\d{4}-(0[1-9]|1[0-2])$/)
      expect(Number.isInteger(entry.count)).toBe(true)
      expect(entry.count).toBeGreaterThanOrEqual(0)
    }

    expect(typedMonthIndex.months.map(({ month }) => month)).toEqual(
      [...typedMonthIndex.months.map(({ month }) => month)].sort().reverse(),
    )
  })
})
