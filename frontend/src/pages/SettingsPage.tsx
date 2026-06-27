import { useApi } from '../lib/utils';
import { api } from '../lib/api';
import { Card, LoadingState, ErrorState, EmptyState } from '../components/ui';

export function SettingsPage() {
  const { data: config, loading: cLoad, error: cErr } = useApi(() => api.config());
  const { data: health, loading: hLoad } = useApi(() => api.dataHealth());

  if (cLoad) return <LoadingState label="Loading settings..." />;
  if (cErr) return <ErrorState message={cErr} />;

  return (
    <>
      <Card title="Configuration" badge={config?.is_sample_data ? 'DEMO' : 'LIVE'}>
        {config ? (
          <div className="detail-grid">
            <DetailRow label="Config Version" value={config.config_version.toString()} />
            <DetailRow label="Broker Mode" value={config.broker_mode} />
            <DetailRow label="Paper Mode" value={config.paper_mode} />
            <DetailRow label="Kill Switch" value={config.order_kill_switch ? 'ENGAGED' : 'off'} />
            <DetailRow label="Gamma URL" value={config.gamma_base_url} />
            <DetailRow label="CLOB URL" value={config.clob_base_url} />
            <DetailRow label="Fill Fee Rate" value={`${(config.fill_fee_rate * 100).toFixed(2)}%`} />
            <DetailRow label="Review Delay" value={`${config.review_delay_seconds}s`} />
            <DetailRow label="Conservative Marks" value={config.use_conservative_mark ? 'yes' : 'no'} />
            <DetailRow label="Staleness" value={`${config.staleness_seconds}s`} />
            <DetailRow label="Dedup Window" value={`${config.dedup_window_seconds}s`} />
            <DetailRow label="Copy Threshold" value={config.score_copy_threshold.toString()} />
            <DetailRow label="Watchlist Threshold" value={config.score_watchlist_threshold.toString()} />
            <DetailRow label="HTTP Timeout" value={`${config.http_timeout_seconds}s`} />
            <DetailRow label="Rate Limit" value={`${config.http_rate_limit_rps} rps`} />
            <DetailRow label="Log Level" value={config.log_level} />
            <DetailRow label="Hash Algo" value={config.snapshot_hash_algo} />
          </div>
        ) : (
          <EmptyState message="Config unavailable" />
        )}
      </Card>

      <Card title="Exposure Limits">
        {config ? (
          <div className="detail-grid">
            <DetailRow label="Per Market" value={config.max_exposure_per_market > 0 ? config.max_exposure_per_market.toString() : 'unlimited'} />
            <DetailRow label="Per Wallet" value={config.max_exposure_per_wallet > 0 ? config.max_exposure_per_wallet.toString() : 'unlimited'} />
            <DetailRow label="Per Outcome" value={config.max_exposure_per_outcome > 0 ? config.max_exposure_per_outcome.toString() : 'unlimited'} />
            <DetailRow label="Global" value={config.max_exposure_global > 0 ? config.max_exposure_global.toString() : 'unlimited'} />
            <DetailRow label="Max Order Size" value={config.max_order_size > 0 ? config.max_order_size.toString() : 'unlimited'} />
          </div>
        ) : (
          <EmptyState message="Limits unavailable" />
        )}
      </Card>

      <Card title="Data Source Health" badge={health?.overall_status ?? 'unknown'}>
        {hLoad ? (
          <LoadingState />
        ) : health ? (
          <table className="table">
            <thead>
              <tr>
                <th>Source</th>
                <th>Status</th>
                <th>Details</th>
              </tr>
            </thead>
            <tbody>
              {health.sources.map((s) => (
                <tr key={s.source}>
                  <td>{s.source}</td>
                  <td>
                    <span className={`tag tag--${s.status === 'ok' ? 'ok' : s.status === 'stale' ? 'stale' : 'unavail'}`}>
                      {s.status}
                    </span>
                  </td>
                  <td style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>{s.details}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState message="Health data unavailable" />
        )}
      </Card>

      <Card title="About">
        <div style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', lineHeight: 1.6 }}>
          <p><strong>Polycopy</strong> v0.2.0 — Paper Trading Platform for Polymarket</p>
          <p style={{ marginTop: 8 }}>
            <strong>Safety guarantees:</strong> No real-money execution path exists.
            Live broker adapters fail closed. All sample/fixture data is visibly labeled.
          </p>
          <p style={{ marginTop: 8 }}>
            <strong>Reduced motion:</strong> This UI respects prefers-reduced-motion.
            No animations simulate live data flow.
          </p>
        </div>
      </Card>
    </>
  );
}

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <>
      <div className="detail-grid__label">{label}</div>
      <div className="detail-grid__value">{value}</div>
    </>
  );
}
