import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { DataHealthPage } from '../pages/DataHealthPage';
import { makeDataHealth } from '../test/fixtures';

const { api } = vi.hoisted(() => ({
  api: {
    dataHealth: vi.fn(),
  },
}))

vi.mock('../lib/api', () => ({
  api,
}))

describe('DataHealthPage', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders loading state', () => {
    api.dataHealth.mockReturnValue(new Promise(() => {}))
    render(
      <MemoryRouter>
        <DataHealthPage />
      </MemoryRouter>,
    )
    expect(screen.getByRole('status')).toBeInTheDocument()
  })

  it('shows empty state when no health data', async () => {
    api.dataHealth.mockResolvedValue(null)
    render(
      <MemoryRouter>
        <DataHealthPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/no health data/i)).toBeInTheDocument()
  })

  it('renders source status table', async () => {
    api.dataHealth.mockResolvedValue(makeDataHealth({
      sources: [
        { source: 'polymarket_clob', last_fetched_at: '2026-06-27T12:00:00Z', status: 'ok', details: 'Connected' },
      ],
    }))
    render(
      <MemoryRouter>
        <DataHealthPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText('ok')).toBeInTheDocument()
    expect(screen.getByText('polymarket_clob')).toBeInTheDocument()
  })

  it('shows overall status KPI', async () => {
    api.dataHealth.mockResolvedValue(makeDataHealth({ overall_status: 'ok' }))
    render(
      <MemoryRouter>
        <DataHealthPage />
      </MemoryRouter>,
    )
    await screen.findByText('polymarket_clob')
    // Overall status badge shows OK
    expect(screen.getAllByText('OK').length).toBeGreaterThan(0)
  })

  it('shows error state on API failure', async () => {
    api.dataHealth.mockRejectedValue(new Error('Health endpoint unreachable'))
    render(
      <MemoryRouter>
        <DataHealthPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/health endpoint unreachable/i)).toBeInTheDocument()
  })

  it('displays snapshot count', async () => {
    api.dataHealth.mockResolvedValue(makeDataHealth({ snapshot_count: 100 }))
    render(
      <MemoryRouter>
        <DataHealthPage />
      </MemoryRouter>,
    )
    await screen.findByText('ok')
    expect(screen.getByText('100')).toBeInTheDocument()
  })
})
