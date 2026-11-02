"""Allow ``python -m leaneval`` as well as the installed entry point."""

from __future__ import annotations

from leaneval.cli import main

raise SystemExit(main())
