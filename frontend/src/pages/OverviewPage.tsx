import { useApi, formatCurrency, formatPercent } from '../lib/utils';
import { api } from '../lib/api';
import { Card, LoadingState, ErrorState, EmptyState } from '../components/ui';

export function OverviewPage() {
  const { data: summary, loading: sLoad, error: sErr } = useApi(() => api.portfolioSummary());
  const { data: status, loading: stLoad } = useApi(() => api.systemStatus());
  const { data: health } = useApi(() => api.health());
  const { data: scanners, loading: scLoad } = useApi(() => api.scans(10));
  const { data: signals, loading: siLoad } = useApi(() => api.signals(10));

  if (sLoad && stLoad) return <LoadingState />;
  if (sErr) return <ErrorState message={sErr} />;

  const sampleLabel = summary?.is_sample_data ? '[DEMO]' : '';

  return (
    <>
      <div className="kpi-grid">
        <div className="kpi">
          <div className="kpi__label">Total Positions {sampleLabel}</div>
          <div className="kpi__value">{summary?.total_positions ?? 0}</div>
          <div className="kpi__sub">{status?.paper_mode ?? '—'}</div>
        </div>
        <div className="kpi">
          <div className="kpi__label">Net PnL {sampleLabel}</div>
          <div className={`kpi__value ${(summary?.total_pnl ?? 0) >= 0 ? 'kpi__value--pos' : 'kpi__value--neg'}`}>
            {formatCurrency(summary?.total_pnl ?? 0)}
          </div>
          <div className="kpi__sub">unrealized: {formatCurrency(summary?.total_unrealized_pnl ?? 0)}</div>
        </div>
        <div className="kpi">
          <div className="kpi__label">Wallets</div>
          <div className="kpi__value">{summary?.wallet_count ?? 0}</div>
          <div className="kpi__sub">tracked</div>
        </div>
        <div className="kpi">
          <div className="kpi__label">Market Value {sampleLabel}</div>
          <div className="kpi__value">{formatCurrency(summary?.total_market_value ?? 0)}</div>
          <div className="kpi__sub">cost: {formatCurrency(summary?.total_cost_basis ?? 0)}</div>
        </div>
      </div>

      <Card title="System Status" badge={health?.is_sample_data ? 'DEMO' : 'LIVE'}>
        {stLoad ? (
          <LoadingState label="Loading status..." />
        ) : status ? (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))', gap: '8px' }}>
            <StatusItem label="Broker" value={status.broker_mode} />
            <StatusItem label="Paper Mode" value={status.paper_mode} />
            <StatusItem label="Kill Switch" value={status.order_kill_switch ? 'ENGAGED' : 'off'} warn={status.order_kill_switch} />
            <StatusItem label="API Version" value={status.config_version.toString()} />
            <StatusItem label="HTTP Timeout" value={`${status.http_timeout_seconds}s`} />
            <StatusItem label="Rate Limit" value={`${status.http_rate_limit_rps} rps`} />
          </div>
        ) : (
          <EmptyState message="Status unavailable" />
        )}
      </Card>

      <Card title="Top Wallets" badge={scanners?.is_sample_data ? 'DEMO' : 'SCANNED'}>
        {scLoad ? (
          <LoadingState label="Loading wallets..." />
        ) : scanners && scanners.scans.length > 0 ? (
          <table className="table table--responsive">
            <thead>
              <tr>
                <th>Label</th>
                <th>Score</th>
                <th>Verdict</th>
                <th>Sources</th>
              </tr>
            </thead>
            <tbody>
              {scanners.scans.map((w) => (
                <tr key={w.address}>
                  <td data-label="Label">
                    <span className="text-mono" style={{ fontSize: '0.75rem' }}>
                      {w.address.slice(0, 10)}…
                    </span>
                    <br />
                    <span style={{ color: 'var(--text-muted)', fontSize: '0.72rem' }}>{w.label}</span>
                  </td>
                  <td data-label="Score">{w.score?.toFixed(1) ?? '—'}</td>
                  <td data-label="Verdict">
                    {w.verdict ? <span className={`verdict verdict--${w.verdict.toLowerCase()}`}>{w.verdict}</span> : '—'}
                  </td>
                  <td data-label="Sources">{w.source_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState message="No wallets scanned" />
        )}
      </Card>

      <Card title="Recent Signals" badge={signals?.is_sample_data ? 'DEMO' : 'LIVE'}>
        {siLoad ? (
          <LoadingState label="Loading signals..." />
        ) : signals && signals.signals.length > 0 ? (
          <table className="table table--responsive">
            <thead>
              <tr>
                <th>Market</th>
                <th>Strength</th>
                <th>Confidence</th>
                <th>Edge</th>
              </tr>
            </thead>
            <tbody>
              {signals.signals.slice(0, 5).map((s) => (
                <tr key={s.id}>
                  <td data-label="Market">
                    <span className="text-mono" style={{ fontSize: '0.72rem' }}>
                      {s.market_id.slice(0, 12)}…
                    </span>
                  </td>
                  <td data-label="Strength">{s.strength}</td>
                  <td data-label="Confidence">{formatPercent(s.confidence)}</td>
                  <td data-label="Edge">{formatPercent(s.edge_estimate)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState message="No signals detected" />
        )}
      </Card>
    </>
  );
}

function StatusItem({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return (
    <div style={{ padding: '8px 12px', background: 'var(--bg-elevated)', borderRadius: 'var(--radius-sm)' }}>
      <div style={{ fontSize: '0.68rem', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{label}</div>
      <div style={{ fontFamily: 'var(--font-mono)', fontSize: '0.85rem', color: warn ? 'var(--danger)' : 'var(--text-primary)', marginTop: '2px' }}>{value}</div>
    </div>
  );
}
