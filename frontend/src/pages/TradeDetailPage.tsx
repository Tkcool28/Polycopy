import { useParams, Link } from 'react-router-dom';
import { useApi, formatPercent, formatDateTime } from '../lib/utils';
import { api } from '../lib/api';
import { Card, LoadingState, EmptyState } from '../components/ui';

export function TradeDetailPage() {
  const { signalId } = useParams();
  const { data: signals } = useApi(() => api.signals(50));

  // We fetch signals list and find the one matching the ID
  // (The API doesn't have a single-signal detail endpoint yet)
  const signal = signals?.signals.find((s) => s.id === signalId);

  if (!signals) return <LoadingState label="Loading signal..." />;
  if (!signal) return <EmptyState message="Signal not found. It may have been generated with demo data." />;

  // Observed facts: directly from API response
  const observed = [
    { label: 'Market ID', value: signal.market_id },
    { label: 'Source', value: signal.source },
    { label: 'Strength', value: signal.strength },
    { label: 'Outcome', value: signal.predicted_prob > signal.market_prob ? 'Yes (buy)' : 'No (sell)' },
    { label: 'Detected At', value: formatDateTime(signal.produced_at) },
  ];

  // Calculated metrics: derived from observed data
  const calculated = [
    { label: 'Confidence', value: formatPercent(signal.confidence), note: 'from model' },
    { label: 'Edge Estimate', value: formatPercent(signal.edge_estimate), note: 'predicted − market' },
    { label: 'Predicted Prob', value: formatPercent(signal.predicted_prob), note: 'model output' },
    { label: 'Market Prob', value: formatPercent(signal.market_prob), note: 'current price' },
    { label: 'Edge Delta', value: formatPercent(signal.predicted_prob - signal.market_prob), note: 'calculated' },
  ];

  // Inferred: heuristic interpretations
  const inferred = [
    { label: 'Signal Quality', value: signal.confidence > 0.7 ? 'Strong' : signal.confidence > 0.5 ? 'Moderate' : 'Weak' },
    { label: 'Direction', value: signal.strength === 'buy' ? 'Bullish' : signal.strength === 'sell' ? 'Bearish' : 'Neutral' },
    { label: 'Risk Level', value: signal.edge_estimate > 0.1 ? 'High edge / higher uncertainty' : 'Conservative' },
  ];

  // Unknown: fields not yet available
  const unknown = [
    { label: 'Source Trade ID', value: 'Not available — signal is generated, not observed from a specific trade' },
    { label: 'Wallet Address', value: 'Not available — signals are market-level, not wallet-level' },
    { label: 'Historical Accuracy', value: 'Not available — no backtest data for this signal source yet' },
    { label: 'Slippage Estimate', value: 'Not available — requires order book depth data' },
  ];

  return (
    <>
      <Link to="/radar" style={{ display: 'inline-block', marginBottom: 12, fontSize: '0.85rem' }}>
        ← Back to Radar
      </Link>

      <Card title="Trade Signal Detail" badge={signal.is_sample ? 'DEMO' : 'LIVE'}>
        <div className="detail-grid">
          <div className="detail-grid__label">Signal ID</div>
          <div className="detail-grid__value">{signal.id}</div>

          <div className="detail-grid__label">Reasoning</div>
          <div className="detail-grid__value" style={{ fontFamily: 'var(--font-sans)', lineHeight: 1.5 }}>
            {signal.reasoning}
          </div>
        </div>
      </Card>

      <div className="detail-section">
        <h3 className="detail-section__title detail-section__title--observed">
          <span style={{ color: 'var(--success)' }}>●</span> Observed Facts
        </h3>
        <p style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginBottom: 8 }}>
          Values returned directly by the API. No interpretation applied.
        </p>
        <Card>
          <div className="detail-grid">
            {observed.map((item) => (
              <ObservedItem key={item.label} {...item} />
            ))}
          </div>
        </Card>
      </div>

      <div className="detail-section">
        <h3 className="detail-section__title detail-section__title--calculated">
          <span style={{ color: 'var(--accent)' }}>●</span> Calculated Metrics
        </h3>
        <p style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginBottom: 8 }}>
          Derived from observed values via arithmetic. Formula documented.
        </p>
        <Card>
          <div className="detail-grid">
            {calculated.map((item) => (
              <CalculatedItem key={item.label} {...item} />
            ))}
          </div>
        </Card>
      </div>

      <div className="detail-section">
        <h3 className="detail-section__title detail-section__title--inferred">
          <span style={{ color: 'var(--warning)' }}>●</span> Inferred / Heuristic
        </h3>
        <p style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginBottom: 8 }}>
          Interpretations based on observed + calculated values. May be wrong.
        </p>
        <Card>
          <div className="detail-grid">
            {inferred.map((item) => (
              <InferredItem key={item.label} {...item} />
            ))}
          </div>
        </Card>
      </div>

      <div className="detail-section">
        <h3 className="detail-section__title detail-section__title--unknown">
          <span style={{ color: 'var(--text-muted)' }}>●</span> Unknown / Unavailable
        </h3>
        <p style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginBottom: 8 }}>
          Data not provided by any endpoint. Do not guess these values.
        </p>
        <Card>
          <div className="detail-grid">
            {unknown.map((item) => (
              <UnknownItem key={item.label} {...item} />
            ))}
          </div>
        </Card>
      </div>
    </>
  );
}

function ObservedItem({ label, value }: { label: string; value: string }) {
  return (
    <>
      <div className="detail-grid__label">{label}</div>
      <div className="detail-grid__value">{value}</div>
    </>
  );
}

function CalculatedItem({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <>
      <div className="detail-grid__label">{label}</div>
      <div className="detail-grid__value">
        {value}
        <span style={{ color: 'var(--text-muted)', fontSize: '0.72rem', marginLeft: 8 }}>({note})</span>
      </div>
    </>
  );
}

function InferredItem({ label, value }: { label: string; value: string }) {
  return (
    <>
      <div className="detail-grid__label">{label}</div>
      <div className="detail-grid__value" style={{ color: 'var(--warning)' }}>{value}</div>
    </>
  );
}

function UnknownItem({ label, value }: { label: string; value: string }) {
  return (
    <>
      <div className="detail-grid__label">{label}</div>
      <div className="detail-grid__value" style={{ color: 'var(--text-muted)', fontStyle: 'italic' }}>{value}</div>
    </>
  );
}
