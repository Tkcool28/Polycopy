import { Link } from 'react-router-dom';
import { useApi, formatPercent, formatDateTime } from '../lib/utils';
import { api } from '../lib/api';
import { Card, LoadingState, ErrorState, EmptyState } from '../components/ui';

export function TradeRadarPage() {
  const { data: signals, loading: sLoad, error: sErr } = useApi(() => api.signals(50));
  const { data: scans, loading: scLoad } = useApi(() => api.scans(50));

  if (sLoad && scLoad) return <LoadingState label="Loading radar..." />;
  if (sErr) return <ErrorState message={sErr} />;

  return (
    <>
      <Card title="Trade Radar" badge={signals?.is_sample_data ? 'DEMO' : 'LIVE'}>
        <p style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginBottom: 12 }}>
          Detected trading signals from tracked wallets. Tap a signal for detail.
        </p>
        {signals && signals.signals.length > 0 ? (
          <table className="table table--responsive">
            <thead>
              <tr>
                <th>Market</th>
                <th>Strength</th>
                <th>Confidence</th>
                <th>Edge</th>
                <th>Detected</th>
              </tr>
            </thead>
            <tbody>
              {signals.signals.map((s) => (
                <tr key={s.id}>
                  <td data-label="Market">
                    <Link to={`/radar/${s.id}`} style={{ color: 'var(--accent)' }}>
                      <span className="text-mono" style={{ fontSize: '0.75rem' }}>
                        {s.market_id.slice(0, 12)}…
                      </span>
                    </Link>
                  </td>
                  <td data-label="Strength">{s.strength}</td>
                  <td data-label="Confidence">{formatPercent(s.confidence)}</td>
                  <td data-label="Edge">{formatPercent(s.edge_estimate)}</td>
                  <td data-label="Detected">{formatDateTime(s.produced_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState message="No signals detected. Run a scan to generate signals." />
        )}
      </Card>

      <Card title="Copy Candidates" badge={scans?.is_sample_data ? 'DEMO' : 'SCANNED'}>
        {scans && scans.scans.length > 0 ? (
          <table className="table table--responsive">
            <thead>
              <tr>
                <th>Wallet</th>
                <th>Score</th>
                <th>Verdict</th>
              </tr>
            </thead>
            <tbody>
              {scans.scans
                .filter((s) => s.verdict === 'copy_candidate')
                .map((s) => (
                  <tr key={s.address}>
                    <td data-label="Wallet">
                      <span className="text-mono" style={{ fontSize: '0.75rem' }}>
                        {s.address.slice(0, 10)}…
                      </span>
                    </td>
                    <td data-label="Score">{s.score?.toFixed(1) ?? '—'}</td>
                    <td data-label="Verdict">
                      <span className={`verdict verdict--${(s.verdict ?? 'incomplete').toLowerCase()}`}>
                        {s.verdict}
                      </span>
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        ) : (
          <EmptyState message="No copy candidates yet." />
        )}
      </Card>
    </>
  );
}
