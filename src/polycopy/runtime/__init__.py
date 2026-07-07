"""Polycopy runtime utilities.

This package contains cross-cutting runtime helpers used by operational
scripts (collect, scan, settle, update) and the API. Subpackages:

- ``locks``: shared global no-overlap lock for operational jobs (PR24D).
- ``memory``: lightweight RSS watchdog for long-running scripts (PR24B).
- ``query_batches``: bounded query iteration helpers (PR24B).
"""