import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { WalletsPage } from '../pages/WalletsPage';
import { makeWallets } from '../test/fixtures';

const { api } = vi.hoisted(() => ({
  api: {
    wallets: vi.fn(),
    walletDetail: vi.fn(),
  },
}))

vi.mock('../lib/api', () => ({
  api,
}))

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return {
    ...actual,
    useParams: () => ({ id: undefined }),
  };
});

describe('WalletsPage', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders loading state before data arrives', () => {
    api.wallets.mockReturnValue(new Promise(() => {}))
    render(
      <MemoryRouter>
        <WalletsPage />
      </MemoryRouter>,
    )
    expect(screen.getByRole('status')).toBeInTheDocument()
  })

  it('shows empty state when no wallets exist', async () => {
    api.wallets.mockResolvedValue(makeWallets({ wallets: [], total_count: 0 }))
    render(
      <MemoryRouter>
        <WalletsPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/no wallets found/i)).toBeInTheDocument()
  })

  it('renders persisted wallet with correct label and score', async () => {
    const wallets = makeWallets({
      wallets: [
        {
          id: 'w-001',
          address: '0xABCDEF1234567890',
          label: 'Smart Trader',
          balances: [{ currency: 'USDC', amount: 1000, as_of: '2026-06-27T00:00:00Z', is_sample: false }],
          is_sample: false,
        },
      ],
      is_sample_data: false,
    });
    api.wallets.mockResolvedValue(wallets)
    render(
      <MemoryRouter>
        <WalletsPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText('Smart Trader')).toBeInTheDocument()
    expect(screen.getByText('1,000.00')).toBeInTheDocument()
  })

  it('displays demo badge when data is sample', async () => {
    api.wallets.mockResolvedValue(makeWallets({ is_sample_data: true }))
    render(
      <MemoryRouter>
        <WalletsPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/DEMO/i)).toBeInTheDocument()
  })

  it('shows error state on API failure', async () => {
    api.wallets.mockRejectedValue(new Error('Network error'))
    render(
      <MemoryRouter>
        <WalletsPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/network error/i)).toBeInTheDocument()
  })
})
