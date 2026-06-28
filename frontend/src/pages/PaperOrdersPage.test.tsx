import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import userEvent from '@testing-library/user-event';
import { PaperOrdersPage } from '../pages/PaperOrdersPage';
import { makeOrders } from '../test/fixtures';

const { api } = vi.hoisted(() => ({
  api: {
    paperOrders: vi.fn(),
    systemStatus: vi.fn(),
    paperPreview: vi.fn(),
    paperApprove: vi.fn(),
    paperReject: vi.fn(),
  },
}))

vi.mock('../lib/api', () => ({
  api,
}))

describe('PaperOrdersPage', () => {
  beforeEach(() => vi.clearAllMocks())

  it('shows paper-order preview section with disclaimer', async () => {
    api.paperOrders.mockResolvedValue(makeOrders())
    api.systemStatus.mockResolvedValue({ paper_mode: 'paper_manual' })
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
    api.systemStatus.mockResolvedValue({ paper_mode: 'paper_manual' })
    render(
      <MemoryRouter>
        <PaperOrdersPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText('pending')).toBeInTheDocument()
  })

  it('shows approve/reject actions for pending orders', async () => {
    api.paperOrders.mockResolvedValue(makeOrders())
    api.systemStatus.mockResolvedValue({ paper_mode: 'paper_manual' })
    render(
      <MemoryRouter>
        <PaperOrdersPage />
      </MemoryRouter>,
    )
    await screen.findByText('pending')
    expect(screen.getByRole('button', { name: /approve/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /reject/i })).toBeInTheDocument()
  })

  it('opens preview result after clicking Preview (mocked API call)', async () => {
    api.paperOrders.mockResolvedValue(makeOrders())
    api.systemStatus.mockResolvedValue({ paper_mode: 'paper_manual', is_sample_data: true })
    api.paperPreview.mockResolvedValue({
      market_id: '00000000-0000-0000-0000-000000000010',
      outcome: 'Yes',
      side: 'buy',
      quantity: 10,
      price: 0.65,
      estimated_fill_price: 0.66,
      estimated_fee: 0.33,
      estimated_total_cost: 6.93,
      is_sample: true,
    })

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
    api.systemStatus.mockResolvedValue({ paper_mode: 'paper_manual' })
    render(
      <MemoryRouter>
        <PaperOrdersPage />
      </MemoryRouter>,
    )
    await screen.findByText('pending')
    expect(screen.getByText(/\[DEMO\]/i)).toBeInTheDocument()
  })

  it('approve sends notes from input field', async () => {
    api.paperOrders.mockResolvedValue(makeOrders())
    api.systemStatus.mockResolvedValue({ paper_mode: 'paper_manual' })
    api.paperApprove.mockResolvedValue({ status: 'ok' })
    const user = userEvent.setup()

    render(
      <MemoryRouter>
        <PaperOrdersPage />
      </MemoryRouter>,
    )

    await screen.findByText('pending')
    // Type a note
    const textarea = screen.getByPlaceholderText(/Add a note/i);
    await user.type(textarea, 'My custom approval note')
    await user.click(screen.getByRole('button', { name: /approve/i }))
    expect(api.paperApprove).toHaveBeenCalledWith({
      order_id: '00000000-0000-0000-0000-000000000001',
      notes: 'My custom approval note',
    })
  })

  it('reject sends notes from input field', async () => {
    api.paperOrders.mockResolvedValue(makeOrders())
    api.systemStatus.mockResolvedValue({ paper_mode: 'paper_manual' })
    api.paperReject.mockResolvedValue({ status: 'ok' })
    const user = userEvent.setup()

    render(
      <MemoryRouter>
        <PaperOrdersPage />
      </MemoryRouter>,
    )

    await screen.findByText('pending')
    // Type a note
    const textarea = screen.getByPlaceholderText(/Add a note/i);
    await user.type(textarea, 'My custom rejection note')
    await user.click(screen.getByRole('button', { name: /reject/i }))
    expect(api.paperReject).toHaveBeenCalledWith({
      order_id: '00000000-0000-0000-0000-000000000001',
      notes: 'My custom rejection note',
    })
  })
})
