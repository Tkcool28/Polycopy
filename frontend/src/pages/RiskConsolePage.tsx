import { useApi, formatCurrency } from '../lib/utils';
import { api } from '../lib/api';
import { Card, LoadingState, ErrorState } from '../components/ui';

export function RiskConsolePage() {
  const { data: risk, loading, error } = useApi(() => api.riskConsole());

  if (loading) return <LoadingState label="Loading risk console..." />;
  if (error) return <ErrorState message={error} />;

  if (!risk) return <ErrorState message="Risk console data unavailable" />;

  const blockedGates = risk.gates.filter((g) => g.verdict === 'blocked');
  const passGates = risk.gates.filter((g) => g.verdict === 'pass');

  return (
    <>
      <Card title="Risk Console" badge={risk.is_sample_data ? 'DEMO' : 'LIVE'}>
        <div style={{ fontSize: '0.78rem', color: 'var(--text-secondary)', marginBottom: 12, lineHeight: 1.5 }}>
          All risk gates are fail-closed. Any error or missing data blocks the order.
          No real risk evaluation is performed — this displays current configuration state.
        </div>

        {risk.kill_switch_active && (
          <div style={{ padding: 12, background: 'rgba(239, 68, 68, 0.15)', border: '1px solid var(--danger)', borderRadius: 'var(--radius-sm)', marginBottom: 12 }}>
            <strong style={{ color: 'var(--danger)' }}>⚠ KILL SWITCH ENGAGED</strong>
            <div style={{ fontSize: '0.78rem', color: 'var(--text-secondary)', marginTop: 4 }}>
              All order creation is blocked. Disengage the kill switch to resume.
            </div>
          </div>
        )}

        <div className="kpi-grid" style={{ marginBottom: 16 }}>
          <div className="kpi">
            <div className="kpi__label">Paper Mode</div>
            <div className="kpi__value">{risk.paper_mode}</div>
          </div>
          <div className="kpi">
            <div className="kpi__label">Gates Passed</div>
            <div className="kpi__value kpi__value--pos">{passGates.length}</div>
          </div>
          <div className="kpi">
            <div className="kpi__label">Gates Blocked</div>
            <div className={`kpi__value ${blockedGates.length > 0 ? 'kpi__value--neg' : 'kpi__value--pos'}`}>
              {blockedGates.length}
            </div>
          </div>
          <div className="kpi">
            <div className="kpi__label">Sample Data</div>
            <div className="kpi__value">{risk.is_sample_data ? 'YES' : 'LIVE'}</div>
          </div>
        </div>
      </Card>

      <Card title="Gate Status" badge={risk.is_sample_data ? 'DEMO' : 'LIVE'}>
        <table className="table table--responsive">
          <thead>
            <tr>
              <th>Gate</th>
              <th>Verdict</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {risk.gates.map((g) => (
              <tr key={g.gate_name}>
                <td data-label="Gate">
                  <span className="text-mono" style={{ fontSize: '0.78rem' }}>
                    {g.gate_name}
                  </span>
                </td>
                <td data-label="Verdict">
                  <span className={`tag tag--${g.verdict === 'pass' ? 'ok' : 'danger'}`}>
                    {g.verdict}
                  </span>
                </td>
                <td data-label="Reason" style={{ fontSize: '0.78rem', fontFamily: 'var(--font-sans)' }}>
                  {g.reason}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <Card title="Exposure Limits" badge={risk.is_sample_data ? 'DEMO' : 'CONFIG'}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))', gap: 12 }}>
          {Object.entries(risk.exposure_limits).map(([key, limit]) => {
            const current = risk.current_exposures[key.replace('max_', '').replace('max_per_', 'per_')] ?? 0;
            const pct = limit > 0 ? (current / limit) * 100 : 0;
            return (
              <div key={key} style={{ padding: 12, background: 'var(--bg-elevated)', borderRadius: 'var(--radius-sm)' }}>
                <div style={{ fontSize: '0.68rem', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                  {key.replace(/_/g, ' ')}
                </div>
                <div style={{ fontFamily: 'var(--font-mono)', fontSize: '0.85rem', color: 'var(--text-primary)', marginTop: 2 }}>
                  {limit > 0 ? formatCurrency(limit) : 'unlimited'}
                </div>
                {limit > 0 && (
                  <div style={{ marginTop: 6 }}>
                    <div style={{ height: 4, background: 'var(--bg-base)', borderRadius: 2, overflow: 'hidden' }}>
                      <div style={{
                        width: `${Math.min(pct, 100)}%`,
                        height: '100%',
                        background: pct > 80 ? 'var(--danger)' : pct > 50 ? 'var(--warning)' : 'var(--success)',
                      }} />
                    </div>
                    <div style={{ fontSize: '0.68rem', color: 'var(--text-muted)', marginTop: 2 }}>
                      Current: {formatCurrency(current)} ({pct.toFixed(1)}%)
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </Card>

      <Card title="Risk Rules" badge="POLICY">
        <div style={{ fontSize: '0.78rem', color: 'var(--text-secondary)', lineHeight: 1.7 }}>
          <p><strong>Kill Switch:</strong> When engaged, ALL order creation is blocked. Cannot be bypassed.</p>
          <p style={{ marginTop: 6 }}><strong>Paper Mode research_only:</strong> Read-only mode. No order creation allowed.</p>
          <p style={{ marginTop: 6 }}><strong>Paper Mode paper_manual:</strong> Orders require explicit operator confirmation.</p>
          <p style={{ marginTop: 6 }}><strong>Paper Mode paper_auto:</strong> Orders fill automatically after gates pass.</p>
          <p style={{ marginTop: 6 }}><strong>Exposure Limits:</strong> Per-market, per-wallet, per-outcome, and global caps. Zero = unlimited.</p>
          <p style={{ marginTop: 6 }}><strong>Fail-closed:</strong> Any error, missing data, or gate failure blocks the order.</p>
          <p style={{ marginTop: 6, color: 'var(--warning)' }}>
            All values shown are configuration state only. No live risk evaluation is performed in this endpoint.
          </p>
        </div>
      </Card>
    </>
  );
}
