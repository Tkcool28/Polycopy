"""Polycopy runtime utilities.

This package contains cross-cutting runtime helpers used by operational
scripts (collect, scan, settle, update) and the API. Subpackages:

- ``locks``: shared global no-overlap lock for operational jobs (PR24D).
"""