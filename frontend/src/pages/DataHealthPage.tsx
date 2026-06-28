import { Link } from 'react-router-dom';
import { useApi, formatDateTime } from '../lib/utils';
import { api } from '../lib/api';
import { Card, LoadingState, ErrorState, EmptyState } from '../components/ui';

export function DataHealthPage() {
  const { data: health, loading, error } = useApi(() => api.dataHealth());

  if (loading) return <LoadingState label="Checking data health..." />;
  if (error) return <ErrorState message={error} />;
  if (!health) return <EmptyState message="No health data available" />;

  const staleSources = health.sources.filter((s) => s.status === 'stale');
  const okSources = health.sources.filter((s) => s.status === 'ok');
  const errSources = health.sources.filter((s) => s.status !== 'ok' && s.status !== 'stale');

  return (
    <>
      <Link to="/settings" style={{ display: 'inline-block', marginBottom: 12, fontSize: '0.85rem' }}>
        ← Back to Settings
      </Link>

      <Card title="Data Source Health" badge={health.overall_status.toUpperCase()}>
        <div className="kpi-grid" style={{ marginBottom: 16 }}>
          <div className="kpi">
            <div className="kpi__label">Overall Status</div>
            <div className={`kpi__value ${health.overall_status === 'ok' ? 'kpi__value--pos' : health.overall_status === 'stale' ? '' : 'kpi__value--neg'}`}>
              {health.overall_status.toUpperCase()}
            </div>
          </div>
          <div className="kpi">
            <div className="kpi__label">Total Snapshots</div>
            <div className="kpi__value">{health.snapshot_count}</div>
          </div>
          <div className="kpi">
            <div className="kpi__label">Sources OK</div>
            <div className="kpi__value kpi__value--pos">{okSources.length}</div>
          </div>
          <div className="kpi">
            <div className="kpi__label">Stale / Error</div>
            <div className={`kpi__value ${staleSources.length + errSources.length > 0 ? 'kpi__value--neg' : 'kpi__value--pos'}`}>
              {staleSources.length + errSources.length}
            </div>
          </div>
        </div>

        <div style={{ fontSize: '0.78rem', color: 'var(--text-muted)', lineHeight: 1.6 }}>
          <p>Freshness thresholds are configured by <code>staleness_seconds</code> in system config.</p>
          <p style={{ marginTop: 4 }}>
            Oldest snapshot: {health.oldest_snapshot ? formatDateTime(health.oldest_snapshot) : '—'}
          </p>
          <p style={{ marginTop: 4 }}>
            Newest snapshot: {health.newest_snapshot ? formatDateTime(health.newest_snapshot) : '—'}
          </p>
        </div>
      </Card>

      <Card title="Source Details" badge={health.overall_status === 'ok' ? 'OK' : health.overall_status.toUpperCase()}>
        {health.sources.length === 0 ? (
          <EmptyState message="No sources configured" />
        ) : (
          <table className="table table--responsive">
            <thead>
              <tr>
                <th>Source</th>
                <th>Status</th>
                <th>Last Fetched</th>
                <th>Details</th>
              </tr>
            </thead>
            <tbody>
              {health.sources.map((s) => (
                <tr key={s.source}>
                  <td data-label="Source">
                    <span className="text-mono" style={{ fontSize: '0.78rem' }}>
                      {s.source}
                    </span>
                  </td>
                  <td data-label="Status">
                    <span className={`tag tag--${s.status === 'ok' ? 'ok' : s.status === 'stale' ? 'stale' : 'unavail'}`}>
                      {s.status}
                    </span>
                  </td>
                  <td data-label="Last Fetched" style={{ fontSize: '0.78rem', color: 'var(--text-muted)' }}>
                    {s.last_fetched_at ? formatDateTime(s.last_fetched_at) : '—'}
                  </td>
                  <td data-label="Details" style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
                    {s.details}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <Card title="Freshness Policy" badge="CONFIG">
        <div style={{ fontSize: '0.78rem', color: 'var(--text-secondary)', lineHeight: 1.7 }}>
          <p><strong>ok:</strong> Source fetched successfully within the configured staleness threshold.</p>
          <p style={{ marginTop: 6 }}><strong>stale:</strong> Source has data but it exceeds the staleness threshold. Consider running a new scan.</p>
          <p style={{ marginTop: 6 }}><strong>error / unavailable:</strong> Source failed to fetch or is not reachable. Check connectivity.</p>
          <p style={{ marginTop: 6, color: 'var(--warning)' }}>
            This page displays read-only status. No mutations are performed.
          </p>
        </div>
      </Card>
    </>
  );
}
