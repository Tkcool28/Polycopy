import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { OverviewPage } from '../pages/OverviewPage'
import { makePortfolioSummary, makeSystemStatus, makeSignals, makeScans } from '../test/fixtures'

const { api } = vi.hoisted(() => ({
  api: {
    portfolioSummary: vi.fn(),
    systemStatus: vi.fn(),
    health: vi.fn(),
    scans: vi.fn(),
    signals: vi.fn(),
  },
}))

vi.mock('../lib/api', () => ({
  api,
}))

describe('OverviewPage', () => {
  beforeEach(() => vi.clearAllMocks())

  it('shows kill switch warning in status when engaged [incomplete-data indicator]', async () => {
    api.portfolioSummary.mockResolvedValue(makePortfolioSummary())
    api.systemStatus.mockResolvedValue(makeSystemStatus({ order_kill_switch: true }))
    api.health.mockResolvedValue({ status: 'ok', version: '0.2.0', is_sample_data: true })
    api.scans.mockResolvedValue(makeScans())
    api.signals.mockResolvedValue(makeSignals())
    render(
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText('ENGAGED')).toBeInTheDocument()
  })

  it('renders verdict labels in the Top Wallets table', async () => {
    api.portfolioSummary.mockResolvedValue(makePortfolioSummary())
    api.systemStatus.mockResolvedValue(makeSystemStatus())
    api.health.mockResolvedValue({ status: 'ok', version: '0.2.0', is_sample_data: true })
    api.scans.mockResolvedValue(makeScans())
    api.signals.mockResolvedValue(makeSignals())
    render(
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText('copy_candidate')).toBeInTheDocument()
  })

  it('labels demo/sample data visibly with [DEMO] tag', async () => {
    api.portfolioSummary.mockResolvedValue(makePortfolioSummary({ is_sample_data: true }))
    api.systemStatus.mockResolvedValue(makeSystemStatus())
    api.health.mockResolvedValue({ status: 'ok', version: '0.2.0', is_sample_data: true })
    api.scans.mockResolvedValue(makeScans())
    api.signals.mockResolvedValue(makeSignals())
    render(
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>,
    )
    await screen.findByText('copy_candidate')
    expect(screen.getAllByText(/\[DEMO\]/i).length).toBeGreaterThan(0)
  })

  it('renders loading state before data arrives', () => {
    api.portfolioSummary.mockReturnValue(new Promise(() => {}))
    api.systemStatus.mockReturnValue(new Promise(() => {}))
    render(
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>,
    )
    expect(screen.getByRole('status')).toBeInTheDocument()
  })
})
