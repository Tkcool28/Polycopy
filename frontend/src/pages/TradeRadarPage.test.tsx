import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { TradeRadarPage } from '../pages/TradeRadarPage'
import { makeSignals, makeScans } from '../test/fixtures'

const { api } = vi.hoisted(() => ({
  api: {
    signals: vi.fn(),
    scans: vi.fn(),
  },
}))

vi.mock('../lib/api', () => ({
  api,
}))

describe('TradeRadarPage', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders verdict labels for copy candidates', async () => {
    api.signals.mockResolvedValue(makeSignals())
    api.scans.mockResolvedValue(makeScans())
    render(
      <MemoryRouter>
        <TradeRadarPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText('copy_candidate')).toBeInTheDocument()
  })

  it('shows empty state when no scans exist', async () => {
    api.signals.mockResolvedValue(makeSignals())
    api.scans.mockResolvedValue(makeScans({ scans: [], total_count: 0 }))
    render(
      <MemoryRouter>
        <TradeRadarPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/no copy candidates/i)).toBeInTheDocument()
  })
})
