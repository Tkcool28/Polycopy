import '@testing-library/jest-dom'
import { beforeEach, vi } from 'vitest'

// [SAMPLE] Mock fetch for all tests — no real network calls
beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn())
})
