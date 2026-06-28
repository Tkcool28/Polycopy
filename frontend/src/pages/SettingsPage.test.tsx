import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { SettingsPage } from '../pages/SettingsPage';
import { makeConfig, makeDataHealth } from '../test/fixtures';

const { api } = vi.hoisted(() => ({
  api: {
    config: vi.fn(),
    dataHealth: vi.fn(),
  },
}))

vi.mock('../lib/api', () => ({
  api,
}))

describe('SettingsPage', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders loading state', () => {
    api.config.mockReturnValue(new Promise(() => {}))
    api.dataHealth.mockResolvedValue(makeDataHealth())
    render(
      <MemoryRouter>
        <SettingsPage />
      </MemoryRouter>,
    )
    expect(screen.getByRole('status')).toBeInTheDocument()
  })

  it('shows error state on API failure', async () => {
    api.config.mockRejectedValue(new Error('Config unavailable'))
    render(
      <MemoryRouter>
        <SettingsPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/config unavailable/i)).toBeInTheDocument()
  })

  it('renders config fields from API data', async () => {
    api.config.mockResolvedValue(makeConfig({
      paper_mode: 'paper_manual',
      max_exposure_global: 1000,
    }))
    api.dataHealth.mockResolvedValue(makeDataHealth())
    render(
      <MemoryRouter>
        <SettingsPage />
      </MemoryRouter>,
    )
    await screen.findByText('paper_manual')
    // Exposure limits card uses .toString() so shows "1000"
    expect(screen.getByText('1000')).toBeInTheDocument()
  })

  it('renders exposure limits (unlimited when zero)', async () => {
    api.config.mockResolvedValue(makeConfig({
      max_exposure_per_market: 0,
      max_exposure_global: 0,
    }))
    api.dataHealth.mockResolvedValue(makeDataHealth())
    render(
      <MemoryRouter>
        <SettingsPage />
      </MemoryRouter>,
    )
    await screen.findByText('paper_manual')
    expect(screen.getAllByText('unlimited').length).toBeGreaterThan(0)
  })

  it('renders data health table sources', async () => {
    api.config.mockResolvedValue(makeConfig())
    api.dataHealth.mockResolvedValue(makeDataHealth({
      sources: [
        { source: 'polymarket_clob', last_fetched_at: '2026-06-27T12:00:00Z', status: 'ok', details: 'Connected' },
        { source: 'gamma_api', last_fetched_at: '2026-06-26T00:00:00Z', status: 'stale', details: 'Data 2h old' },
      ],
    }))
    render(
      <MemoryRouter>
        <SettingsPage />
      </MemoryRouter>,
    )
    await screen.findByText('paper_manual')
    // Verify both source tags are rendered
    const okTags = document.querySelectorAll('.tag--ok')
    const staleTags = document.querySelectorAll('.tag--stale')
    expect(okTags.length).toBe(1)
    expect(staleTags.length).toBe(1)
  })
})
