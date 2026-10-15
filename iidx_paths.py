"""Shared external writable paths for Python and frozen versions."""
import os
import sys
from pathlib import Path

def app_root():
    override = os.environ.get("IIDX_APP_ROOT")
    if override:
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent
