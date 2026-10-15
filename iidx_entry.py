"""Single-entry mode router for IIDX RANDOM (stage 1: Python smoke test).

Frozen packaging and external resource paths are handled in later stages.
"""
import argparse
import runpy
import sys
import os
from pathlib import Path

MODES = {
    'launcher': 'iidx_launcher',
    'analyzer': 'iidx_main',
    'detection-band': 'Tool.iidx_detection_band_settings',
    'random-settings': 'Tool.iidx_random_overlay_settings',
    'history-settings': 'Tool.iidx_history_overlay_settings',
    'song-editor': 'data.iidx_song_title_editor',
}


def main():
    parser = argparse.ArgumentParser(description='IIDX RANDOM single entry point')
    parser.add_argument('--mode', choices=tuple(MODES), default='launcher')
    args, forwarded = parser.parse_known_args()
    # Keep the analyzer's existing argparse interface (--launcher-mode, etc.).
    if getattr(sys, "frozen", False):
        os.environ["IIDX_APP_ROOT"] = str(Path(sys.executable).resolve().parent)
    # In a frozen Windows GUI executable, the child process may inherit a
    # non-UTF-8 stdout encoding even when its output is piped to the launcher.
    # Reconfigure only when stdout exists (windowed builds may have None).
    if getattr(sys, "frozen", False) and sys.stdout is not None:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace", line_buffering=True)
        except (AttributeError, OSError, ValueError):
            pass
    sys.argv = [sys.argv[0], *forwarded]
    runpy.run_module(MODES[args.mode], run_name='__main__', alter_sys=True)


if __name__ == '__main__':
    main()
