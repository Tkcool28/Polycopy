import { useParams, Link } from 'react-router-dom';
import { useApi, formatCurrency, formatDateTime } from '../lib/utils';
import { api } from '../lib/api';
import { Card, LoadingState, ErrorState, EmptyState } from '../components/ui';

export function WalletsPage() {
  const { id } = useParams();
  const { data: wallets, loading, error } = useApi(() => api.wallets(50));

  if (loading) return <LoadingState label="Loading wallets..." />;
  if (error) return <ErrorState message={error} />;
  if (!wallets || wallets.wallets.length === 0) {
    return <EmptyState message="No wallets found. Run a scan to discover wallets." />;
  }

  // If an ID is in the URL, show detail
  if (id) {
    const wallet = wallets.wallets.find((w) => w.id === id);
    if (!wallet) {
      // Try fetching detail
      return <WalletDetailView walletId={id} />;
    }
    return <WalletDetail wallet={wallet} />;
  }

  return (
    <>
      <Card title="Smart Wallet Leaderboard" badge={wallets.is_sample_data ? 'DEMO' : 'SCANNED'}>
        <table className="table table--responsive">
          <thead>
            <tr>
              <th>Wallet</th>
              <th>Balance</th>
              <th>Currency</th>
              <th>As Of</th>
            </tr>
          </thead>
          <tbody>
            {wallets.wallets.map((w) => (
              <tr key={w.id}>
                <td data-label="Wallet">
                  <Link to={`/wallets/${w.id}`} style={{ color: 'var(--accent)' }}>
                    <span className="text-mono" style={{ fontSize: '0.75rem' }}>
                      {w.address.slice(0, 10)}…{w.address.slice(-4)}
                    </span>
                  </Link>
                  <br />
                  <span style={{ color: 'var(--text-muted)', fontSize: '0.72rem' }}>{w.label}</span>
                </td>
                <td data-label="Balance">
                  {w.balances.length > 0
                    ? formatCurrency(w.balances[0].amount, w.balances[0].currency)
                    : '—'}
                </td>
                <td data-label="Currency">
                  {w.balances.length > 0 ? w.balances[0].currency : '—'}
                </td>
                <td data-label="As Of">
                  {w.balances.length > 0 ? formatDateTime(w.balances[0].as_of) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </>
  );
}

function WalletDetail({ wallet }: { wallet: import('../lib/types').WalletDetailView }) {
  return (
    <>
      <Link to="/wallets" style={{ display: 'inline-block', marginBottom: 12, fontSize: '0.85rem' }}>
        ← Back to Wallets
      </Link>
      <Card title="Wallet Detail" badge={wallet.is_sample ? 'DEMO' : 'LIVE'}>
        <div className="detail-grid">
          <div className="detail-grid__label">Address</div>
          <div className="detail-grid__value">{wallet.address}</div>

          <div className="detail-grid__label">Label</div>
          <div className="detail-grid__value">{wallet.label}</div>

          <div className="detail-grid__label">Internal ID</div>
          <div className="detail-grid__value">{wallet.id}</div>
        </div>
      </Card>

      <Card title="Balances">
        {wallet.balances.length === 0 ? (
          <EmptyState message="No balance data" />
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Currency</th>
                <th className="text-right">Amount</th>
                <th>As Of</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {wallet.balances.map((b, i) => (
                <tr key={i}>
                  <td>{b.currency}</td>
                  <td className="text-right">{formatCurrency(b.amount, b.currency)}</td>
                  <td>{formatDateTime(b.as_of)}</td>
                  <td>{b.is_sample ? <span className="tag tag--sample">DEMO</span> : <span className="tag tag--ok">LIVE</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  );
}

function WalletDetailView({ walletId }: { walletId: string }) {
  const { data: wallet, loading, error } = useApi(() => api.walletDetail(walletId), [walletId]);

  if (loading) return <LoadingState label="Loading wallet..." />;
  if (error) return <ErrorState message={error} />;
  if (!wallet) return <EmptyState message="Wallet not found" />;

  return <WalletDetail wallet={wallet} />;
}
