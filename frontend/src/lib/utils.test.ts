import { describe, it, expect } from 'vitest'
import { formatCurrency, formatPercent, formatNumber, cn } from '../lib/utils'

describe('utils — pure formatters', () => {
  it('formats currency with 2 decimals', () => {
    expect(formatCurrency(12.5)).toBe('12.50')
    expect(formatCurrency(0)).toBe('0.00')
    expect(formatCurrency(-3.1)).toBe('-3.10')
  })

  it('formats percent by multiplying by 100', () => {
    expect(formatPercent(0.65)).toBe('65.0%')
    expect(formatPercent(0)).toBe('0.0%')
    expect(formatPercent(1)).toBe('100.0%')
  })

  it('formats numbers with locale grouping', () => {
    expect(formatNumber(1000)).toBe('1,000')
    expect(formatNumber(0)).toBe('0')
  })

  it('cn joins truthy class names', () => {
    expect(cn('a', 'b', false, null, undefined, 'c')).toBe('a b c')
    expect(cn('x')).toBe('x')
    expect(cn(false, null)).toBe('')
  })
})
