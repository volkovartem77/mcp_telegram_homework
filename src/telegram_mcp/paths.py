"""Shared local paths; importing this module does not open a session."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SESSION_PATH = PROJECT_ROOT / "data" / "telegram.session"
