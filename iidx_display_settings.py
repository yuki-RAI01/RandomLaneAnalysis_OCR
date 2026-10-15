"""History display preferences sourced from the per-side history overlay config.

OBS compatibility: the published history_visible/history_visibility fields are
preserved. The old display_settings.json is no longer read or written.
"""
from iidx_paths import app_root
import json
from pathlib import Path

SETTINGS_FILENAME = 'iidx_history_overlay_config.json'
PHASES = ('playing', 'ended', 'selecting')


def settings_path(base_dir=None):
    base = Path(base_dir) if base_dir is not None else app_root()
    return base / 'config' / SETTINGS_FILENAME


def default_settings():
    return {'side_profiles': {side: {'visibility': {phase: True for phase in PHASES}}
                              for side in ('1P', '2P')}}


def load_settings(path=None):
    path = Path(path) if path is not None else settings_path()
    settings = default_settings()
    if not path.exists():
        return settings
    try:
        raw = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(raw, dict):
            raise ValueError('settings root must be an object')
        profiles = raw.get('side_profiles', {})
        if not isinstance(profiles, dict):
            raise ValueError('side_profiles must be an object')
        for side in ('1P', '2P'):
            profile = profiles.get(side, {})
            if not isinstance(profile, dict):
                raise ValueError(f'side_profiles.{side} must be an object')
            visibility = profile.get('visibility', {})
            if not isinstance(visibility, dict):
                raise ValueError(f'side_profiles.{side}.visibility must be an object')
            for phase in PHASES:
                if phase in visibility:
                    value = visibility[phase]
                    if not isinstance(value, bool):
                        raise ValueError(f'side_profiles.{side}.visibility.{phase} must be boolean')
                    settings['side_profiles'][side]['visibility'][phase] = value
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f'履歴表示設定を読み込めません: {path}\n{exc}') from exc
    return settings


def phase_to_key(phase):
    return {'PLAYING': 'playing', 'CONFIRMED': 'playing', 'PRE_PLAY': 'playing',
            'ENDED': 'ended', 'IDLE': 'selecting', 'SELECTING': 'selecting'}.get(str(phase).upper())


def history_visible(settings, phase, side='1P'):
    key = phase_to_key(phase)
    if key is None:
        return False
    side = side if side in ('1P', '2P') else '1P'
    return bool(settings['side_profiles'][side]['visibility'].get(key, True))


def history_visibility(settings, side='1P'):
    side = side if side in ('1P', '2P') else '1P'
    return dict(settings['side_profiles'][side]['visibility'])
