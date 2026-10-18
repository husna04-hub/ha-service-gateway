"""``python -m ha_gateway`` entry point."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
