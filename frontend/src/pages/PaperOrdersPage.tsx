import { useState } from 'react';
import { useApi, formatCurrency, formatPercent } from '../lib/utils';
import { api } from '../lib/api';
import { Card, LoadingState, ErrorState, EmptyState } from '../components/ui';

export function PaperOrdersPage() {
  const { data: orders, loading, error, refetch } = useApi(() => api.paperOrders());
  const { data: status } = useApi(() => api.systemStatus());
  const [previewData, setPreviewData] = useState<import('../lib/types').PaperOrderPreview | null>(null);
  const [note, setNote] = useState('');
  const [actionResult, setActionResult] = useState<string | null>(null);

  // Preview form state
  const [marketId, setMarketId] = useState('00000000-0000-0000-0000-000000000010');
  const [outcome, setOutcome] = useState('Yes');
  const [side, setSide] = useState<'buy' | 'sell'>('buy');
  const [quantity, setQuantity] = useState(10);
  const [price, setPrice] = useState(0.65);

  const handlePreview = async () => {
    try {
      const data = await api.paperPreview({
        market_id: marketId,
        outcome,
        side,
        quantity,
        price,
      });
      setPreviewData(data);
      setActionResult(null);
    } catch (e) {
      setActionResult(`Preview failed: ${(e as Error).message}`);
    }
  };

  const handleApprove = async (orderId: string) => {
    try {
      const result = await api.paperApprove({ order_id: orderId });
      if (result?.status !== 'error') {
        setActionResult(`Order ${orderId.slice(0, 8)}… APPROVED (${status?.paper_mode ?? 'paper'}) ${previewData?.is_sample ? '[SAMPLE]' : ''}`);
        setNote('');
        refetch();
      } else {
        setActionResult(`Approve failed: ${result?.detail ?? 'unknown error'}`);
      }
    } catch (e) {
      setActionResult(`Approve failed: ${(e as Error).message}`);
    }
  };

  const handleReject = async (orderId: string) => {
    try {
      const result = await api.paperReject({ order_id: orderId });
      if (result?.status !== 'error') {
        setActionResult(`Order ${orderId.slice(0, 8)}… REJECTED (${status?.paper_mode ?? 'paper'})`);
        refetch();
      } else {
        setActionResult(`Reject failed: ${result?.detail ?? 'unknown error'}`);
      }
    } catch (e) {
      setActionResult(`Reject failed: ${(e as Error).message}`);
    }
  };

  if (loading) return <LoadingState label="Loading paper orders..." />;
  if (error) return <ErrorState message={error} />;

  return (
    <>
      <Card title="Paper Order Preview" badge="PAPER MANUAL">
        <p style={{ fontSize: '0.78rem', color: 'var(--text-muted)', marginBottom: 12 }}>
          Preview shows estimated fill, fees, and total cost. No real trade is executed.
          All orders require explicit approval in paper_manual mode.
        </p>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: '8px', marginBottom: 12 }}>
          <label style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
            Market ID
            <input value={marketId} onChange={(e) => setMarketId(e.target.value)}
              style={inputStyle} />
          </label>
          <label style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
            Outcome
            <select value={outcome} onChange={(e) => setOutcome(e.target.value)}
              style={inputStyle}>
              <option value="Yes">Yes</option>
              <option value="No">No</option>
            </select>
          </label>
          <label style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
            Side
            <select value={side} onChange={(e) => setSide(e.target.value as 'buy' | 'sell')}
              style={inputStyle}>
              <option value="buy">Buy</option>
              <option value="sell">Sell</option>
            </select>
          </label>
          <label style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
            Quantity
            <input type="number" value={quantity} min={1} onChange={(e) => setQuantity(Number(e.target.value))}
              style={inputStyle} />
          </label>
          <label style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
            Price (0-1)
            <input type="number" value={price} min={0} max={1} step={0.01} onChange={(e) => setPrice(Number(e.target.value))}
              style={inputStyle} />
          </label>
        </div>
        <button onClick={handlePreview} style={btnStyle}>
          Preview Order
        </button>

        {previewData && (
          <div style={{ marginTop: 16, padding: 12, background: 'var(--bg-elevated)', borderRadius: 'var(--radius-sm)' }}>
            <h4 style={{ fontSize: '0.85rem', marginBottom: 8 }}>Preview Result {previewData?.is_sample ? ' [SAMPLE DATA]' : ` — ${status?.paper_mode ?? 'paper'}`}</h4>
            <div className="detail-grid">
              <div className="detail-grid__label">Est. Fill Price</div>
              <div className="detail-grid__value">{formatPercent(previewData.estimated_fill_price)}</div>
              <div className="detail-grid__label">Est. Fee</div>
              <div className="detail-grid__value">{formatCurrency(previewData.estimated_fee)}</div>
              <div className="detail-grid__label">Total Cost</div>
              <div className="detail-grid__value">{formatCurrency(previewData.estimated_total_cost)}</div>
            </div>
            <p style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginTop: 8 }}>
              Risk gates: All pass (sample). In live mode, exposure limits, kill switch, and mode checks apply.
            </p>
          </div>
        )}
      </Card>

      <Card title="Pending Orders" badge={`${orders?.total_count ?? 0} ${orders?.is_sample_data ? '[DEMO]' : ''}`}>
        {orders && orders.orders.length > 0 ? (
          <>
            {actionResult && (
              <div style={{ padding: '8px 12px', background: 'var(--bg-elevated)', borderRadius: 'var(--radius-sm)', marginBottom: 12, fontSize: '0.78rem' }}>
                {actionResult}
              </div>
            )}
            <table className="table table--responsive">
              <thead>
                <tr>
                  <th>Market</th>
                  <th>Outcome</th>
                  <th>Side</th>
                  <th className="text-right">Qty</th>
                  <th className="text-right">Price</th>
                  <th>Status</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {orders.orders.map((o) => (
                  <tr key={o.id}>
                    <td data-label="Market">
                      <span className="text-mono" style={{ fontSize: '0.72rem' }}>
                        {o.market_id.slice(0, 12)}…
                      </span>
                    </td>
                    <td data-label="Outcome">{o.outcome}</td>
                    <td data-label="Side">{o.side}</td>
                    <td data-label="Qty" className="text-right">{o.quantity}</td>
                    <td data-label="Price" className="text-right">{formatPercent(o.price)}</td>
                    <td data-label="Status">
                      <span className={`tag tag--${o.status === 'pending' ? 'watch' : o.status === 'accepted' ? 'ok' : 'skip'}`}>
                        {o.status}
                      </span>
                    </td>
                    <td data-label="Actions">
                      {o.status === 'pending' && (
                        <div style={{ display: 'flex', gap: 4 }}>
                          <button onClick={() => handleApprove(o.id)} style={btnSmStyle('ok')}>
                            Approve
                          </button>
                          <button onClick={() => handleReject(o.id)} style={btnSmStyle('danger')}>
                            Reject
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        ) : (
          <EmptyState message="No pending paper orders" />
        )}
      </Card>

      <Card title="Manual Action Note" badge="AUDIT">
        <div style={{ fontSize: '0.78rem', color: 'var(--text-secondary)', marginBottom: 8 }}>
          Notes are stored locally for audit. They do not affect order execution.
        </div>
        <textarea
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="Add a note about this decision (optional, stored locally only)..."
          style={{ ...inputStyle, width: '100%', minHeight: 60, resize: 'vertical' }}
        />
      </Card>
    </>
  );
}

const inputStyle: React.CSSProperties = {
  display: 'block',
  width: '100%',
  padding: '6px 8px',
  marginTop: 4,
  background: 'var(--bg-base)',
  border: '1px solid var(--border)',
  borderRadius: 'var(--radius-sm)',
  color: 'var(--text-primary)',
  fontFamily: 'var(--font-mono)',
  fontSize: '0.8rem',
};

const btnStyle: React.CSSProperties = {
  padding: '8px 16px',
  background: 'var(--accent)',
  color: 'var(--bg-base)',
  border: 'none',
  borderRadius: 'var(--radius-sm)',
  cursor: 'pointer',
  fontFamily: 'var(--font-sans)',
  fontSize: '0.82rem',
  fontWeight: 600,
};

function btnSmStyle(kind: 'ok' | 'danger'): React.CSSProperties {
  const bg = kind === 'ok' ? 'var(--success)' : 'var(--danger)';
  return {
    padding: '4px 8px',
    background: bg,
    color: '#fff',
    border: 'none',
    borderRadius: 'var(--radius-sm)',
    cursor: 'pointer',
    fontSize: '0.72rem',
    fontWeight: 600,
  };
}
