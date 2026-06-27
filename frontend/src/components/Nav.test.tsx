import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Nav } from '../components/Nav'

describe('Nav', () => {
  it('renders all primary navigation items', () => {
    render(
      <MemoryRouter>
        <Nav />
      </MemoryRouter>,
    )
    expect(screen.getByRole('navigation', { name: /main navigation/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /overview/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /wallets/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /risk/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /orders/i })).toBeInTheDocument()
  })

  it('marks icons as aria-hidden so screen readers skip decoration', () => {
    render(
      <MemoryRouter>
        <Nav />
      </MemoryRouter>,
    )
    const hidden = document.querySelectorAll('[aria-hidden="true"]')
    expect(hidden.length).toBeGreaterThan(0)
  })

  it('uses nav links that are keyboard-focusable (a11y)', () => {
    render(
      <MemoryRouter>
        <Nav />
      </MemoryRouter>,
    )
    const links = screen.getAllByRole('link')
    links.forEach((link) => {
      expect(link).toHaveAttribute('href')
    })
  })
})
