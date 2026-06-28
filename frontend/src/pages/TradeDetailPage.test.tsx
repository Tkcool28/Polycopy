import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { TradeDetailPage } from '../pages/TradeDetailPage';
import { makeSignals } from '../test/fixtures';

const { api } = vi.hoisted(() => ({
  api: {
    signals: vi.fn(),
  },
}))

vi.mock('../lib/api', () => ({
  api,
}))

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return {
    ...actual,
    useParams: () => ({ signalId: 'sig-00000001' }),
  };
});

describe('TradeDetailPage', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders loading state while fetching signals', () => {
    api.signals.mockReturnValue(new Promise(() => {}))
    render(
      <MemoryRouter>
        <TradeDetailPage />
      </MemoryRouter>,
    )
    expect(screen.getByRole('status')).toBeInTheDocument()
  })

  it('shows empty state when signal id not found', async () => {
    api.signals.mockResolvedValue(makeSignals({ signals: [] }))
    render(
      <MemoryRouter>
        <TradeDetailPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/signal not found/i)).toBeInTheDocument()
  })

  it('renders observed facts section from API data', async () => {
    api.signals.mockResolvedValue(makeSignals({
      signals: [{
        id: 'sig-00000001',
        market_id: '00000000-0000-0000-0000-000000000010',
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
        <TradeDetailPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText('Observed Facts')).toBeInTheDocument()
    expect(screen.getByText('smart_money')).toBeInTheDocument()
  })

  it('renders calculated metrics section', async () => {
    api.signals.mockResolvedValue(makeSignals({
      signals: [{
        id: 'sig-00000001',
        market_id: '00000000-0000-0000-0000-000000000010',
        source: 'smart_money',
        strength: 'strong',
        confidence: 0.82,
        edge_estimate: 0.15,
        predicted_prob: 0.7,
        market_prob: 0.55,
        reasoning: 'Reasoning text',
        produced_at: '2026-06-27T12:00:00Z',
        is_sample: false,
      }],
      is_sample_data: false,
    }))
    render(
      <MemoryRouter>
        <TradeDetailPage />
      </MemoryRouter>,
    )
    await screen.findByText('Observed Facts')
    expect(screen.getByText('Calculated Metrics')).toBeInTheDocument()
    // confidence => 82.0%
    expect(screen.getByText('82.0%')).toBeInTheDocument()
  })

  it('renders unknown fields section for missing data', async () => {
    api.signals.mockResolvedValue(makeSignals({
      signals: [{
        id: 'sig-00000001',
        market_id: 'mkt-001',
        source: 'smart_money',
        strength: 'strong',
        confidence: 0.82,
        edge_estimate: 0.15,
        predicted_prob: 0.7,
        market_prob: 0.55,
        reasoning: 'Reasoning text',
        produced_at: '2026-06-27T12:00:00Z',
        is_sample: false,
      }],
      is_sample_data: false,
    }))
    render(
      <MemoryRouter>
        <TradeDetailPage />
      </MemoryRouter>,
    )
    await screen.findByText('Observed Facts')
    expect(screen.getByText('Unknown / Unavailable')).toBeInTheDocument()
    expect(screen.getByText(/source trade id/i)).toBeInTheDocument()
  })

  it('renders inferred / heuristic section', async () => {
    api.signals.mockResolvedValue(makeSignals({
      signals: [{
        id: 'sig-00000001',
        market_id: 'mkt-001',
        source: 'smart_money',
        strength: 'strong',
        confidence: 0.82,
        edge_estimate: 0.15,
        predicted_prob: 0.7,
        market_prob: 0.55,
        reasoning: 'Reasoning text',
        produced_at: '2026-06-27T12:00:00Z',
        is_sample: false,
      }],
      is_sample_data: false,
    }))
    render(
      <MemoryRouter>
        <TradeDetailPage />
      </MemoryRouter>,
    )
    await screen.findByText('Observed Facts')
    expect(screen.getByText('Inferred / Heuristic')).toBeInTheDocument()
    expect(screen.getByText(/signal quality/i)).toBeInTheDocument()
  })
})
