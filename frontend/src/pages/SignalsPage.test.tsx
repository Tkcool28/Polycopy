import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { SignalsPage } from '../pages/SignalsPage';
import { makeSignals } from '../test/fixtures';

const { api } = vi.hoisted(() => ({
  api: {
    signals: vi.fn(),
  },
}))

vi.mock('../lib/api', () => ({
  api,
}))

describe('SignalsPage', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders loading state before data arrives', () => {
    api.signals.mockReturnValue(new Promise(() => {}))
    render(
      <MemoryRouter>
        <SignalsPage />
      </MemoryRouter>,
    )
    expect(screen.getByRole('status')).toBeInTheDocument()
  })

  it('shows empty state when no signals exist', async () => {
    api.signals.mockResolvedValue(makeSignals({ signals: [], total_count: 0 }))
    render(
      <MemoryRouter>
        <SignalsPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/no signals detected/i)).toBeInTheDocument()
  })

  it('renders signal fields from API', async () => {
    api.signals.mockResolvedValue(makeSignals({
      signals: [{
        id: 'sig-001',
        market_id: 'mkt-001',
        source: 'smart_money',
        strength: 'strong',
        confidence: 0.82,
        edge_estimate: 0.15,
        predicted_prob: 0.7,
        market_prob: 0.55,
        reasoning: 'Sample signal reasoning',
        produced_at: '2026-06-27T12:00:00Z',
        is_sample: false,
      }],
      is_sample_data: false,
    }))
    render(
      <MemoryRouter>
        <SignalsPage />
      </MemoryRouter>,
    )
    await screen.findByText('smart_money')
    expect(screen.getByText('strong')).toBeInTheDocument()
    // confidence 0.82 => 82.0%
    expect(screen.getByText('82.0%')).toBeInTheDocument()
  })

  it('shows error state on API failure', async () => {
    api.signals.mockRejectedValue(new Error('Connection refused'))
    render(
      <MemoryRouter>
        <SignalsPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/connection refused/i)).toBeInTheDocument()
  })

  it('labels demo data with DEMO badge', async () => {
    api.signals.mockResolvedValue(makeSignals({ is_sample_data: true }))
    render(
      <MemoryRouter>
        <SignalsPage />
      </MemoryRouter>,
    )
    await screen.findByText('smart_money')
    expect(screen.getByText(/DEMO/i)).toBeInTheDocument()
  })
})
