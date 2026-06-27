/**
 * [SAMPLE] Mock data factories for tests.
 * All values are clearly labeled fixtures — no live data.
 */
import type {
  SystemStatusResponse,
  RiskConsoleResponse,
  OrdersResponse,
  SignalsResponse,
  ScanResponse,
  PortfolioSummary,
} from '../lib/types'

export function makeSystemStatus(overrides: Partial<SystemStatusResponse> = {}): SystemStatusResponse {
  return {
    config_version: 1,
    broker_mode: 'paper',
    paper_mode: 'paper_manual',
    order_kill_switch: false,
    is_live: false,
    db_path: ':memory:',
    http_timeout_seconds: 10,
    http_rate_limit_rps: 1,
    log_level: 'INFO',
    is_sample_data: true,
    ...overrides,
  }
}

export function makePortfolioSummary(overrides: Partial<PortfolioSummary> = {}): PortfolioSummary {
  return {
    total_positions: 3,
    total_cost_basis: 150.0,
    total_market_value: 162.5,
    total_unrealized_pnl: 12.5,
    total_realized_pnl: 0,
    total_pnl: 12.5,
    wallet_count: 5,
    is_sample_data: true,
    ...overrides,
  }
}

export function makeRiskConsole(overrides: Partial<RiskConsoleResponse> = {}): RiskConsoleResponse {
  return {
    kill_switch_active: false,
    paper_mode: 'paper_manual',
    exposure_limits: { max_exposure_per_market: 100, max_exposure_global: 500 },
    current_exposures: { per_market: 25, global: 75 },
    gates: [
      { gate_name: 'kill_switch', verdict: 'pass', reason: 'Kill switch is off', is_sample: true },
      { gate_name: 'research_only', verdict: 'pass', reason: 'Not in research_only mode', is_sample: true },
      { gate_name: 'exposure_cap', verdict: 'blocked', reason: 'Sample block for test coverage', is_sample: true },
    ],
    is_sample_data: true,
    ...overrides,
  }
}

export function makeOrders(overrides: Partial<OrdersResponse> = {}): OrdersResponse {
  return {
    orders: [
      {
        id: '00000000-0000-0000-0000-000000000001',
        market_id: '00000000-0000-0000-0000-000000000010',
        wallet_id: '00000000-0000-0000-0000-000000000099',
        side: 'buy',
        order_type: 'limit',
        outcome: 'Yes',
        quantity: 10,
        price: 0.65,
        status: 'pending',
        filled_quantity: 0,
        signal_id: null,
        created_at: '2026-06-27T00:00:00Z',
        updated_at: null,
        is_sample: true,
      },
    ],
    total_count: 1,
    is_sample_data: true,
    ...overrides,
  }
}

export function makeSignals(overrides: Partial<SignalsResponse> = {}): SignalsResponse {
  return {
    signals: [
      {
        id: 'sig-00000001',
        market_id: '00000000-0000-0000-0000-000000000010',
        source: 'smart_money',
        strength: 'strong',
        confidence: 0.82,
        edge_estimate: 0.15,
        predicted_prob: 0.7,
        market_prob: 0.55,
        reasoning: 'Sample signal reasoning',
        produced_at: '2026-06-27T12:00:00Z',
        is_sample: true,
      },
    ],
    total_count: 1,
    is_sample_data: true,
    ...overrides,
  }
}

export function makeScans(overrides: Partial<ScanResponse> = {}): ScanResponse {
  return {
    scans: [
      {
        address: '0xABCDEF1234567890ABCDEF1234567890ABCDEF12',
        label: 'Sample Whale',
        sources: ['polymarket'],
        source_count: 1,
        score: 8.5,
        verdict: 'copy_candidate',
        is_sample: true,
      },
      {
        address: '0x1111111111111111111111111111111111111111',
        label: 'Low data wallet',
        sources: [],
        source_count: 0,
        score: null,
        verdict: null,
        is_sample: true,
      },
    ],
    total_count: 2,
    is_sample_data: true,
    ...overrides,
  }
}
