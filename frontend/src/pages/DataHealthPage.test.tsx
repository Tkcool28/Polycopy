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

  it('displays backend-shaped last_success_at and last_attempt_at timestamps', async () => {
    api.dataHealth.mockResolvedValue(makeDataHealth({
      sources: [
        {
          source: 'polymarket_gamma',
          last_success_at: '2026-06-27T12:00:00Z',
          last_attempt_at: '2026-06-27T12:05:00Z',
          status: 'ok',
          details: 'Connected',
        },
        {
          source: 'polymarket_clob',
          last_success_at: null,
          last_attempt_at: null,
          status: 'unavailable',
          details: 'No attempts yet',
        },
      ],
    }))

    render(
      <MemoryRouter>
        <DataHealthPage />
      </MemoryRouter>,
    )

    expect(await screen.findByText('polymarket_gamma')).toBeInTheDocument()
    expect(screen.getByText('Last Success')).toBeInTheDocument()
    expect(screen.getByText('Last Attempt')).toBeInTheDocument()
    expect(screen.getAllByText(/Jun 27/).length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText('polymarket_clob')).toBeInTheDocument()
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2)
  })
})
