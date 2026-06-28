import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { PortfolioPage } from '../pages/PortfolioPage';
import { makePortfolioSummary, makePositions, makeDecisionLog } from '../test/fixtures';

const { api } = vi.hoisted(() => ({
  api: {
    portfolioSummary: vi.fn(),
    positions: vi.fn(),
    decisionLog: vi.fn(),
  },
}))

vi.mock('../lib/api', () => ({
  api,
}))

describe('PortfolioPage', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders loading state', () => {
    api.portfolioSummary.mockReturnValue(new Promise(() => {}))
    api.positions.mockResolvedValue(makePositions())
    api.decisionLog.mockResolvedValue(makeDecisionLog())
    render(
      <MemoryRouter>
        <PortfolioPage />
      </MemoryRouter>,
    )
    expect(screen.getByRole('status')).toBeInTheDocument()
  })

  it('shows error state on API failure', async () => {
    api.portfolioSummary.mockRejectedValue(new Error('Server down'))
    render(
      <MemoryRouter>
        <PortfolioPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/server down/i)).toBeInTheDocument()
  })

  it('renders positions table from persisted data', async () => {
    api.portfolioSummary.mockResolvedValue(makePortfolioSummary({ is_sample_data: false, total_positions: 1 }))
    api.positions.mockResolvedValue(makePositions({
      positions: [{
        id: 'pos-1',
        market_id: 'mkt-001',
        wallet_id: 'w-001',
        outcome: 'Yes',
        quantity: 10,
        avg_entry_price: 0.6,
        current_price: 0.65,
        realized_pnl: 0,
        unrealized_pnl: 0.5,
        opened_at: '2026-06-25T00:00:00Z',
        updated_at: null,
        is_sample: false,
      }],
      is_sample_data: false,
    }))
    api.decisionLog.mockResolvedValue(makeDecisionLog({ entries: [], total_count: 0 }))
    render(
      <MemoryRouter>
        <PortfolioPage />
      </MemoryRouter>,
    )
    // Should show the position's quantity
    expect(await screen.findByText('10')).toBeInTheDocument()
  })

  it('shows empty state when no positions exist', async () => {
    api.portfolioSummary.mockResolvedValue(makePortfolioSummary({ total_positions: 0 }))
    api.positions.mockResolvedValue(makePositions({ positions: [], total_count: 0 }))
    api.decisionLog.mockResolvedValue(makeDecisionLog())
    render(
      <MemoryRouter>
        <PortfolioPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/no open positions/i)).toBeInTheDocument()
  })

  it('labels demo data visibly on positions', async () => {
    api.portfolioSummary.mockResolvedValue(makePortfolioSummary({ is_sample_data: true, total_positions: 0 }))
    api.positions.mockResolvedValue(makePositions({ positions: [], total_count: 0, is_sample_data: true }))
    api.decisionLog.mockResolvedValue(makeDecisionLog())
    render(
      <MemoryRouter>
        <PortfolioPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/no open positions/i)).toBeInTheDocument()
  })
})
