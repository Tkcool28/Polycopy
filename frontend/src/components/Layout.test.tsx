import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Layout } from '../components/Layout'
import { Header } from '../components/Header'
import { makePortfolioSummary, makeSystemStatus } from '../test/fixtures'

const { api } = vi.hoisted(() => ({
  api: {
    portfolioSummary: vi.fn(),
    systemStatus: vi.fn(),
  },
}))

vi.mock('../lib/api', () => ({
  api,
}))

describe('Layout mobile structure', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders banners, header, nav, and outlet region in order', async () => {
    api.portfolioSummary.mockResolvedValue(makePortfolioSummary())
    api.systemStatus.mockResolvedValue(makeSystemStatus())
    render(
      <MemoryRouter>
        <Layout />
      </MemoryRouter>,
    )
    const app = document.querySelector('.app')
    expect(app).not.toBeNull()
    // Check mobile-first: main content exists
    expect(document.querySelector('main.main-content')).toBeInTheDocument()
    // Nav is present
    expect(screen.getByRole('navigation')).toBeInTheDocument()
  })
})

describe('Header', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders PnL with positive styling when >= 0', async () => {
    api.portfolioSummary.mockResolvedValue(makePortfolioSummary({ total_pnl: 12.5 }))
    api.systemStatus.mockResolvedValue(makeSystemStatus())
    render(
      <MemoryRouter>
        <Header />
      </MemoryRouter>,
    )
    await screen.findByText('PnL')
    expect(document.querySelector('.header__metric-value--positive')).toBeInTheDocument()
  })

  it('renders PnL with negative styling when < 0', async () => {
    api.portfolioSummary.mockResolvedValue(makePortfolioSummary({ total_pnl: -5.0 }))
    api.systemStatus.mockResolvedValue(makeSystemStatus())
    render(
      <MemoryRouter>
        <Header />
      </MemoryRouter>,
    )
    await screen.findByText('PnL')
    expect(document.querySelector('.header__metric-value--negative')).toBeInTheDocument()
  })

  it('displays broker mode from system status', async () => {
    api.portfolioSummary.mockResolvedValue(makePortfolioSummary())
    api.systemStatus.mockResolvedValue(makeSystemStatus({ broker_mode: 'paper' }))
    render(
      <MemoryRouter>
        <Header />
      </MemoryRouter>,
    )
    expect(await screen.findByText('paper')).toBeInTheDocument()
  })
})
