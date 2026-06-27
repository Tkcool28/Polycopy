import { useApi } from '../lib/utils';
import { api } from '../lib/api';

export function Banners() {
  const { data: status, loading } = useApi(() => api.systemStatus());

  if (loading || !status) return null;

  return (
    <>
      {status.broker_mode === 'paper' && (
        <div className="banner banner--paper">
          PAPER MODE — No real trades. All data is simulated.
        </div>
      )}
      {status.is_sample_data && (
        <div className="banner banner--sample">
          DEMO DATA — Showing sample/fixture data. Not live.
        </div>
      )}
      {status.order_kill_switch && (
        <div className="banner banner--kill">
          KILL SWITCH ACTIVE — All order creation blocked.
        </div>
      )}
    </>
  );
}
