"""Make ``import server`` work from the plugin folder, matching the
other catalog widgets. Run from a Tesserae checkout's venv so
``app.plugin_http`` and Flask resolve."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
