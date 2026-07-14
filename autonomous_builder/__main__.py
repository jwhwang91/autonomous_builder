"""Enable ``python -m autonomous_builder ...``."""
from __future__ import annotations

import sys

from autonomous_builder.cli import main

if __name__ == "__main__":  # pragma: no cover - thin shim
    sys.exit(main())
