const BASE = import.meta.env.VITE_API_BASE ?? '';

async function request<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
  });
  if (!res.ok) {
    throw new Error(`API ${path} failed: ${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => request<import('./types').HealthResponse>('/health'),
  systemStatus: () => request<import('./types').SystemStatusResponse>('/system/status'),
  scans: (limit = 50, offset = 0) =>
    request<import('./types').ScanResponse>(`/scans?limit=${limit}&offset=${offset}`),
  wallets: (limit = 50, offset = 0) =>
    request<import('./types').WalletsResponse>(`/wallets?limit=${limit}&offset=${offset}`),
  walletDetail: (id: string) =>
    request<import('./types').WalletDetailView>(`/wallets/${id}`),
  signals: (limit = 50, offset = 0) =>
    request<import('./types').SignalsResponse>(`/signals?limit=${limit}&offset=${offset}`),
  positions: (walletId?: string) =>
    request<import('./types').PositionsResponse>(
      `/positions${walletId ? `?wallet_id=${walletId}` : ''}`,
    ),
  portfolioSummary: () =>
    request<import('./types').PortfolioSummary>('/portfolio/summary'),
  decisionLog: (limit = 50, offset = 0) =>
    request<import('./types').DecisionLogResponse>(`/decision-log?limit=${limit}&offset=${offset}`),
  decisionLogExport: (format: 'json' | 'csv') =>
    request<import('./types').DecisionLogExportResponse>(`/decision-log/export?format=${format}`),
  experiments: (limit = 50, offset = 0) =>
    request<import('./types').ExperimentMetricsResponse>(`/experiments?limit=${limit}&offset=${offset}`),
  riskConsole: () =>
    request<import('./types').RiskConsoleResponse>('/risk/console'),
  paperOrders: (statusFilter?: string) =>
    request<import('./types').OrdersResponse>(
      `/paper/orders${statusFilter ? `?status=${statusFilter}` : ''}`,
    ),
  config: () => request<import('./types').ConfigView>('/config'),
  dataHealth: () => request<import('./types').DataHealthResponse>('/data/health'),
};
