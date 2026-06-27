import { useApi, formatCurrency, formatPercent, formatDateTime } from '../lib/utils';
import { api } from '../lib/api';
import { Card, LoadingState, ErrorState, EmptyState } from '../components/ui';

export function PortfolioPage() {
  const { data: summary, loading: sumLoad, error: sumErr } = useApi(() => api.portfolioSummary());
  const { data: positions, loading: posLoad } = useApi(() => api.positions());
  const { data: decisions, loading: decLoad } = useApi(() => api.decisionLog(20));

  if (sumLoad) return <LoadingState label="Loading portfolio..." />;
  if (sumErr) return <ErrorState message={sumErr} />;

  return (
    <>
      <div className="kpi-grid">
        <div className="kpi">
          <div className="kpi__label">Market Value</div>
          <div className="kpi__value">{formatCurrency(summary?.total_market_value ?? 0)}</div>
        </div>
        <div className="kpi">
          <div className="kpi__label">Cost Basis</div>
          <div className="kpi__value">{formatCurrency(summary?.total_cost_basis ?? 0)}</div>
        </div>
        <div className="kpi">
          <div className="kpi__label">Unrealized PnL</div>
          <div className={`kpi__value ${(summary?.total_unrealized_pnl ?? 0) >= 0 ? 'kpi__value--pos' : 'kpi__value--neg'}`}>
            {formatCurrency(summary?.total_unrealized_pnl ?? 0)}
          </div>
        </div>
        <div className="kpi">
          <div className="kpi__label">Realized PnL</div>
          <div className={`kpi__value ${(summary?.total_realized_pnl ?? 0) >= 0 ? 'kpi__value--pos' : 'kpi__value--neg'}`}>
            {formatCurrency(summary?.total_realized_pnl ?? 0)}
          </div>
        </div>
      </div>

      <Card title="Open Positions" badge={`${summary?.total_positions ?? 0} ${summary?.is_sample_data ? '[DEMO]' : ''}`}>
        {posLoad ? (
          <LoadingState />
        ) : positions && positions.positions.length > 0 ? (
          <table className="table table--responsive">
            <thead>
              <tr>
                <th>Market</th>
                <th>Outcome</th>
                <th className="text-right">Qty</th>
                <th className="text-right">Entry</th>
                <th className="text-right">Current</th>
                <th className="text-right">Unreal. PnL</th>
              </tr>
            </thead>
            <tbody>
              {positions.positions.map((p) => (
                <tr key={p.id}>
                  <td data-label="Market">
                    <span className="text-mono" style={{ fontSize: '0.72rem' }}>
                      {p.market_id.slice(0, 12)}…
                    </span>
                  </td>
                  <td data-label="Outcome">{p.outcome}</td>
                  <td data-label="Qty" className="text-right">{p.quantity}</td>
                  <td data-label="Entry" className="text-right">{formatPercent(p.avg_entry_price)}</td>
                  <td data-label="Current" className="text-right">{formatPercent(p.current_price)}</td>
                  <td data-label="Unreal. PnL" className={`text-right ${p.unrealized_pnl >= 0 ? 'kpi__value--pos' : 'kpi__value--neg'}`}>
                    {formatCurrency(p.unrealized_pnl)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState message="No open positions" />
        )}
      </Card>

      <Card title="Decision Log" badge={decisions?.is_sample_data ? 'DEMO' : 'LIVE'}>
        {decLoad ? (
          <LoadingState />
        ) : decisions && decisions.entries.length > 0 ? (
          <table className="table table--responsive">
            <thead>
              <tr>
                <th>Type</th>
                <th>Rationale</th>
                <th>Time</th>
              </tr>
            </thead>
            <tbody>
              {decisions.entries.map((e) => (
                <tr key={e.id}>
                  <td data-label="Type">
                    <span className={`tag tag--${e.decision_type === 'skip' ? 'skip' : e.decision_type === 'copy' ? 'copy' : 'watch'}`}>
                      {e.decision_type}
                    </span>
                  </td>
                  <td data-label="Rationale" style={{ fontFamily: 'var(--font-sans)', fontSize: '0.78rem' }}>
                    {e.rationale}
                  </td>
                  <td data-label="Time">{formatDateTime(e.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState message="No decisions recorded" />
        )}
      </Card>
    </>
  );
}
