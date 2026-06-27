import { useApi, formatCurrency } from '../lib/utils';
import { api } from '../lib/api';

export function Header() {
  const { data: summary, loading: sumLoading } = useApi(() => api.portfolioSummary());
  const { data: status, loading: statLoading } = useApi(() => api.systemStatus());

  const pnl = summary?.total_pnl ?? 0;
  const pnlPositive = pnl >= 0;

  return (
    <header className="header">
      <div className="header__logo">
        poly<span>copy</span>
      </div>
      <div className="header__metrics">
        {!sumLoading && summary && (
          <>
            <div className="header__metric">
              <span className="header__metric-label">Positions</span>
              <span className="header__metric-value">{summary.total_positions}</span>
            </div>
            <div className="header__metric">
              <span className="header__metric-label">PnL</span>
              <span className={`header__metric-value ${pnlPositive ? 'header__metric-value--positive' : 'header__metric-value--negative'}`}>
                {formatCurrency(pnl)}
              </span>
            </div>
          </>
        )}
        {!statLoading && status && (
          <div className="header__metric">
            <span className="header__metric-label">Mode</span>
            <span className="header__metric-value">{status.broker_mode}</span>
          </div>
        )}
      </div>
    </header>
  );
}
