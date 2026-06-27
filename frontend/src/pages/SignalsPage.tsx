import { useApi, formatPercent, formatDateTime } from '../lib/utils';
import { api } from '../lib/api';
import { Card, LoadingState, ErrorState, EmptyState } from '../components/ui';

export function SignalsPage() {
  const { data: signals, loading, error } = useApi(() => api.signals(50));

  if (loading) return <LoadingState label="Loading signals..." />;
  if (error) return <ErrorState message={error} />;

  if (!signals || signals.signals.length === 0) {
    return <EmptyState message="No signals detected. Run a scan to generate signals." />;
  }

  return (
    <>
      <Card title="Trading Signals" badge={signals.is_sample_data ? 'DEMO' : 'LIVE'}>
        <table className="table table--responsive">
          <thead>
            <tr>
              <th>ID</th>
              <th>Source</th>
              <th>Strength</th>
              <th className="text-right">Confidence</th>
              <th className="text-right">Predicted</th>
              <th className="text-right">Market</th>
              <th className="text-right">Edge</th>
              <th>Detected</th>
            </tr>
          </thead>
          <tbody>
            {signals.signals.map((s) => (
              <tr key={s.id}>
                <td data-label="ID">
                  <span className="text-mono" style={{ fontSize: '0.72rem' }}>
                    {s.id.slice(0, 8)}…
                  </span>
                </td>
                <td data-label="Source">{s.source}</td>
                <td data-label="Strength">{s.strength}</td>
                <td data-label="Confidence" className="text-right">{formatPercent(s.confidence)}</td>
                <td data-label="Predicted" className="text-right">{formatPercent(s.predicted_prob)}</td>
                <td data-label="Market" className="text-right">{formatPercent(s.market_prob)}</td>
                <td data-label="Edge" className="text-right">{formatPercent(s.edge_estimate)}</td>
                <td data-label="Detected">{formatDateTime(s.produced_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </>
  );
}
