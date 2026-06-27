import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Banners } from '../components/Banners'
import { makeSystemStatus } from '../test/fixtures'

const { api } = vi.hoisted(() => ({
  api: { systemStatus: vi.fn() },
}))

vi.mock('../lib/api', () => ({
  api,
}))

function renderWithRouter(ui: React.ReactElement) {
  return render(<MemoryRouter>{ui}</MemoryRouter>)
}

describe('Banners', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows PAPER MODE banner when broker_mode is paper', async () => {
    api.systemStatus.mockResolvedValue(
      makeSystemStatus({ broker_mode: 'paper', is_sample_data: false }),
    )
    renderWithRouter(<Banners />)
    expect(await screen.findByText(/PAPER MODE/i)).toBeInTheDocument()
  })

  it('shows DEMO DATA banner when is_sample_data is true', async () => {
    api.systemStatus.mockResolvedValue(
      makeSystemStatus({ broker_mode: 'live', is_sample_data: true }),
    )
    renderWithRouter(<Banners />)
    expect(await screen.findByText(/DEMO DATA/i)).toBeInTheDocument()
  })

  it('shows KILL SWITCH banner when order_kill_switch is true', async () => {
    api.systemStatus.mockResolvedValue(
      makeSystemStatus({ order_kill_switch: true }),
    )
    renderWithRouter(<Banners />)
    expect(await screen.findByText(/KILL SWITCH ACTIVE/i)).toBeInTheDocument()
  })

  it('renders nothing while loading', () => {
    api.systemStatus.mockReturnValue(new Promise(() => {}))
    const { container } = renderWithRouter(<Banners />)
    expect(container.innerHTML).toBe('')
  })
})
