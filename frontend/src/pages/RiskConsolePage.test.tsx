import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { RiskConsolePage } from '../pages/RiskConsolePage'
import { makeRiskConsole } from '../test/fixtures'

const { api } = vi.hoisted(() => ({
  api: { riskConsole: vi.fn() },
}))

vi.mock('../lib/api', () => ({
  api,
}))

describe('RiskConsolePage', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders gate verdict labels (pass and blocked)', async () => {
    api.riskConsole.mockResolvedValue(makeRiskConsole())
    render(
      <MemoryRouter>
        <RiskConsolePage />
      </MemoryRouter>,
    )
    expect(await screen.findByText('kill_switch')).toBeInTheDocument()
    // Multiple gates have verdict "pass" — assert at least one
    expect(screen.getAllByText('pass').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('blocked')).toBeInTheDocument()
  })

  it('shows kill switch engaged warning when active', async () => {
    api.riskConsole.mockResolvedValue(makeRiskConsole({ kill_switch_active: true }))
    render(
      <MemoryRouter>
        <RiskConsolePage />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/KILL SWITCH ENGAGED/i)).toBeInTheDocument()
  })

  it('displays gates passed/blocked counts', async () => {
    api.riskConsole.mockResolvedValue(makeRiskConsole())
    const { container } = render(
      <MemoryRouter>
        <RiskConsolePage />
      </MemoryRouter>,
    )
    await screen.findByText('kill_switch')
    // gates: 2 pass + 1 blocked
    expect(container.textContent).toMatch(/2/)
    expect(container.textContent).toMatch(/1/)
  })

  it('labels sample data visibly', async () => {
    api.riskConsole.mockResolvedValue(makeRiskConsole())
    render(
      <MemoryRouter>
        <RiskConsolePage />
      </MemoryRouter>,
    )
    await screen.findByText('kill_switch')
    expect(screen.getAllByText(/DEMO/i).length).toBeGreaterThan(0)
  })
})
