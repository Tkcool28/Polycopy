import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { ExperimentsPage } from '../pages/ExperimentsPage';
import { makeExperiments } from '../test/fixtures';

const { api } = vi.hoisted(() => ({
  api: {
    experiments: vi.fn(),
  },
}))

vi.mock('../lib/api', () => ({
  api,
}))

describe('ExperimentsPage', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders loading state', () => {
    api.experiments.mockReturnValue(new Promise(() => {}))
    render(
      <MemoryRouter>
        <ExperimentsPage />
      </MemoryRouter>,
    )
    expect(screen.getByRole('status')).toBeInTheDocument()
  })

  it('shows empty state when no experiments exist', async () => {
    api.experiments.mockResolvedValue(makeExperiments({ experiments: [], total_count: 0, profitable_count: 0 }))
    render(
      <MemoryRouter>
        <ExperimentsPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/no experiments/i)).toBeInTheDocument()
  })

  it('renders experiment with status badge', async () => {
    api.experiments.mockResolvedValue(makeExperiments())
    render(
      <MemoryRouter>
        <ExperimentsPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText('completed')).toBeInTheDocument()
  })

  it('calculates win rate from data', async () => {
    api.experiments.mockResolvedValue(makeExperiments({
      total_count: 4,
      profitable_count: 3,
    }))
    render(
      <MemoryRouter>
        <ExperimentsPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText('completed'))
    // 3/4 = 75%
    expect(screen.getByText('75%')).toBeInTheDocument()
  })

  it('shows error state on API failure', async () => {
    api.experiments.mockRejectedValue(new Error('DB read error'))
    render(
      <MemoryRouter>
        <ExperimentsPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/db read error/i)).toBeInTheDocument()
  })

  it('labels demo data visibly', async () => {
    api.experiments.mockResolvedValue(makeExperiments({ is_sample_data: true }))
    render(
      <MemoryRouter>
        <ExperimentsPage />
      </MemoryRouter>,
    )
    await screen.findByText('completed')
    expect(screen.getAllByText(/DEMO/i).length).toBeGreaterThan(0)
  })
})
