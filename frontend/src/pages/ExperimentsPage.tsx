import { useApi, formatDateTime } from '../lib/utils';
import { api } from '../lib/api';
import { Card, LoadingState, ErrorState, EmptyState } from '../components/ui';

export function ExperimentsPage() {
  const { data: experiments, loading, error } = useApi(() => api.experiments(50));

  if (loading) return <LoadingState label="Loading experiments..." />;
  if (error) return <ErrorState message={error} />;

  if (!experiments || experiments.experiments.length === 0) {
    return <EmptyState message="No experiments have been run yet." />;
  }

  return (
    <>
      <Card title="Experiments & Metrics" badge={experiments.is_sample_data ? 'DEMO' : 'LIVE'}>
        <p style={{ fontSize: '0.78rem', color: 'var(--text-muted)', marginBottom: 12 }}>
          Experiment runs track strategy configurations, performance, and outcomes.
          All values are sample/fixture data until live experiments are executed.
        </p>
        <div className="kpi-grid" style={{ marginBottom: 16 }}>
          <div className="kpi">
            <div className="kpi__label">Total Experiments</div>
            <div className="kpi__value">{experiments.total_count}</div>
          </div>
          <div className="kpi">
            <div className="kpi__label">Profitable</div>
            <div className={`kpi__value ${experiments.profitable_count > 0 ? 'kpi__value--pos' : ''}`}>
              {experiments.profitable_count}
            </div>
          </div>
          <div className="kpi">
            <div className="kpi__label">Win Rate</div>
            <div className="kpi__value">
              {experiments.total_count > 0
                ? `${((experiments.profitable_count / experiments.total_count) * 100).toFixed(0)}%`
                : '—'}
            </div>
          </div>
          <div className="kpi">
            <div className="kpi__label">Data Source</div>
            <div className="kpi__value">{experiments.is_sample_data ? 'DEMO' : 'LIVE'}</div>
          </div>
        </div>
      </Card>

      <Card title="Experiment Runs" badge={experiments.is_sample_data ? 'DEMO' : 'LIVE'}>
        <table className="table table--responsive">
          <thead>
            <tr>
              <th>Label</th>
              <th>Status</th>
              <th>Started</th>
              <th>Ended</th>
              <th className="text-right">Results</th>
            </tr>
          </thead>
          <tbody>
            {experiments.experiments.map((e) => (
              <tr key={e.id}>
                <td data-label="Label">
                  <span className="text-mono" style={{ fontSize: '0.75rem' }}>
                    {e.id.slice(0, 8)}…
                  </span>
                  <br />
                  <span style={{ color: 'var(--text-muted)', fontSize: '0.72rem' }}>{e.label}</span>
                </td>
                <td data-label="Status">
                  <span className={`tag tag--${e.status === 'completed' ? 'ok' : e.status === 'running' ? 'copy' : 'skip'}`}>
                    {e.status}
                  </span>
                </td>
                <td data-label="Started">{e.started_at ? formatDateTime(e.started_at) : '—'}</td>
                <td data-label="Ended">{e.ended_at ? formatDateTime(e.ended_at) : '—'}</td>
                <td data-label="Results" className="text-right" style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>
                  {e.result_summary ? JSON.stringify(e.result_summary).slice(0, 40) : '—'}
                  {e.error_message && (
                    <div style={{ color: 'var(--danger)', marginTop: 2 }}>{e.error_message}</div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <Card title="Strategy Config" badge={experiments.is_sample_data ? 'DEMO' : 'CONFIG'}>
        {experiments.experiments[0]?.strategy_config ? (
          <div className="detail-grid">
            {Object.entries(experiments.experiments[0].strategy_config).map(([k, v]) => (
              <div key={k}>
                <div className="detail-grid__label">{k}</div>
                <div className="detail-grid__value">{String(v)}</div>
              </div>
            ))}
          </div>
        ) : (
          <EmptyState message="No strategy config available" />
        )}
      </Card>
    </>
  );
}
