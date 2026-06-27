import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import userEvent from '@testing-library/user-event'
import { PaperOrdersPage } from '../pages/PaperOrdersPage'
import { makeOrders } from '../test/fixtures'

const { api } = vi.hoisted(() => ({
  api: { paperOrders: vi.fn() },
}))

vi.mock('../lib/api', () => ({
  api,
}))

describe('PaperOrdersPage', () => {
  beforeEach(() => vi.clearAllMocks())

  it('shows paper-order preview section with disclaimer', async () => {
    api.paperOrders.mockResolvedValue(makeOrders())
    render(
      <MemoryRouter>
        <PaperOrdersPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/Paper Order Preview/i)).toBeInTheDocument()
    expect(screen.getByText(/No real trade is executed/i)).toBeInTheDocument()
  })

  it('displays pending orders with status verdict tag', async () => {
    api.paperOrders.mockResolvedValue(makeOrders())
    render(
      <MemoryRouter>
        <PaperOrdersPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText('pending')).toBeInTheDocument()
  })

  it('shows approve/reject actions for pending orders', async () => {
    api.paperOrders.mockResolvedValue(makeOrders())
    render(
      <MemoryRouter>
        <PaperOrdersPage />
      </MemoryRouter>,
    )
    await screen.findByText('pending')
    expect(screen.getByRole('button', { name: /approve/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /reject/i })).toBeInTheDocument()
  })

  it('opens preview result after clicking Preview (mocked fetch)', async () => {
    api.paperOrders.mockResolvedValue(makeOrders())
    // [SAMPLE] Mock the preview POST response
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockResolvedValue({
      json: async () => ({
        market_id: '00000000-0000-0000-0000-000000000010',
        outcome: 'Yes',
        side: 'buy',
        quantity: 10,
        price: 0.65,
        estimated_fill_price: 0.66,
        estimated_fee: 0.33,
        estimated_total_cost: 6.93,
        is_sample: true,
      }),
      ok: true,
    } as Response)

    const user = userEvent.setup()
    render(
      <MemoryRouter>
        <PaperOrdersPage />
      </MemoryRouter>,
    )

    await screen.findByText('pending')
    await user.click(screen.getByRole('button', { name: /preview order/i }))

    expect(await screen.findByText(/Preview Result/i)).toBeInTheDocument()
    expect(screen.getByText(/\[SAMPLE DATA\]/i)).toBeInTheDocument()
  })

  it('shows DEMO badge on pending orders when is_sample_data is true', async () => {
    api.paperOrders.mockResolvedValue(makeOrders({ is_sample_data: true }))
    render(
      <MemoryRouter>
        <PaperOrdersPage />
      </MemoryRouter>,
    )
    await screen.findByText('pending')
    expect(screen.getByText(/\[DEMO\]/i)).toBeInTheDocument()
  })
})
