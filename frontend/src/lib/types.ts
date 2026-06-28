// API response types matching FastAPI backend responses

export interface HealthResponse {
  status: string;
  version: string;
  is_sample_data: boolean;
}

export interface SystemStatusResponse {
  config_version: number;
  broker_mode: string;
  paper_mode: string;
  order_kill_switch: boolean;
  is_live: boolean;
  db_path: string;
  http_timeout_seconds: number;
  http_rate_limit_rps: number;
  log_level: string;
  is_sample_data: boolean;
}

export interface ScanResult {
  address: string;
  label: string;
  sources: string[];
  source_count: number;
  score: number | null;
  verdict: string | null;
  is_sample: boolean;
}

export interface ScanResponse {
  scans: ScanResult[];
  total_count: number;
  is_sample_data: boolean;
}

export interface WalletBalanceView {
  currency: string;
  amount: number;
  as_of: string;
  is_sample: boolean;
}

export interface WalletDetailView {
  id: string;
  address: string;
  label: string;
  balances: WalletBalanceView[];
  is_sample: boolean;
}

export interface WalletsResponse {
  wallets: WalletDetailView[];
  total_count: number;
  is_sample_data: boolean;
}

export interface SignalView {
  id: string;
  market_id: string;
  source: string;
  strength: string;
  confidence: number;
  edge_estimate: number;
  predicted_prob: number;
  market_prob: number;
  reasoning: string;
  produced_at: string;
  is_sample: boolean;
}

export interface SignalsResponse {
  signals: SignalView[];
  total_count: number;
  is_sample_data: boolean;
}

export interface PositionView {
  id: string;
  market_id: string;
  wallet_id: string;
  outcome: string;
  quantity: number;
  avg_entry_price: number;
  current_price: number;
  realized_pnl: number;
  unrealized_pnl: number;
  opened_at: string;
  updated_at: string | null;
  is_sample: boolean;
}

export interface PositionsResponse {
  positions: PositionView[];
  total_count: number;
  total_unrealized_pnl: number;
  total_cost_basis: number;
  is_sample_data: boolean;
}

export interface PortfolioSummary {
  total_positions: number;
  total_cost_basis: number;
  total_market_value: number;
  total_unrealized_pnl: number;
  total_realized_pnl: number;
  total_pnl: number;
  wallet_count: number;
  is_sample_data: boolean;
}

export interface DecisionLogView {
  id: string;
  wallet_id: string;
  market_id: string;
  decision_type: string;
  signal_ids: string[];
  order_id: string | null;
  rationale: string;
  metrics: Record<string, unknown>;
  created_at: string;
  is_sample: boolean;
}

export interface DecisionLogResponse {
  entries: DecisionLogView[];
  total_count: number;
  is_sample_data: boolean;
}

export interface ConfigView {
  config_version: number;
  broker_mode: string;
  gamma_base_url: string;
  clob_base_url: string;
  paper_mode: string;
  order_kill_switch: boolean;
  max_exposure_per_market: number;
  max_exposure_per_wallet: number;
  max_exposure_per_outcome: number;
  max_exposure_global: number;
  max_order_size: number;
  fill_fee_rate: number;
  review_delay_seconds: number;
  use_conservative_mark: boolean;
  staleness_seconds: number;
  dedup_window_seconds: number;
  score_copy_threshold: number;
  score_watchlist_threshold: number;
  http_timeout_seconds: number;
  http_rate_limit_rps: number;
  log_level: string;
  snapshot_hash_algo: string;
  is_sample_data: boolean;
}

export interface SourceHealthView {
  source: string;
  last_success_at: string | null;
  last_attempt_at: string | null;
  status: string;
  details: string;
}

export interface DataHealthResponse {
  sources: SourceHealthView[];
  snapshot_count: number;
  oldest_snapshot: string | null;
  newest_snapshot: string | null;
  overall_status: string;
}

export interface ExperimentMetricView {
  id: string;
  label: string;
  strategy_config: Record<string, unknown>;
  status: string;
  started_at: string | null;
  ended_at: string | null;
  result_summary: Record<string, unknown>;
  error_message: string | null;
  is_sample: boolean;
}

export interface ExperimentMetricsResponse {
  experiments: ExperimentMetricView[];
  total_count: number;
  profitable_count: number;
  is_sample_data: boolean;
}

export interface RiskGateView {
  gate_name: string;
  verdict: string;
  reason: string;
  is_sample: boolean;
}

export interface RiskConsoleResponse {
  kill_switch_active: boolean;
  paper_mode: string;
  exposure_limits: Record<string, number>;
  current_exposures: Record<string, number>;
  gates: RiskGateView[];
  is_sample_data: boolean;
}

export interface PaperOrderPreview {
  market_id: string;
  outcome: string;
  side: string;
  quantity: number;
  price: number;
  estimated_fill_price: number;
  estimated_fee: number;
  estimated_total_cost: number;
  is_sample: boolean;
}

export interface OrderView {
  id: string;
  market_id: string;
  wallet_id: string;
  side: string;
  order_type: string;
  outcome: string;
  quantity: number;
  price: number;
  status: string;
  filled_quantity: number;
  signal_id: string | null;
  created_at: string;
  updated_at: string | null;
  is_sample: boolean;
}

export interface OrdersResponse {
  orders: OrderView[];
  total_count: number;
  is_sample_data: boolean;
}

export interface DecisionLogExportResponse {
  format: string;
  data?: string;
  entries?: Record<string, unknown>[];
  is_sample_data: boolean;
}
