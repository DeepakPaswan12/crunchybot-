import toml
from pathlib import Path
from typing import Optional, Union

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "input" / "config.toml"

_DEFAULTS = {
    "dev": {
        "Debug": False,
        "Threads": 4,
        "Proxyless": False,
        "SkipPreflight": False,
        "PreflightWorkers": 50,
        "PreflightTimeout": 10,
    },
    "auth": {
        "BasicAuth": "Basic ZXZ4YzVybGN1bnd4cm91YWpmeHI6NkJGWGM1SUk3UWx2Z3NFbzdiVjBuWUNfN1VRLXVlSVM=",
        "AppVersion": "3.70.0 (22358)",
    },
}

def load_config(path: Optional[Union[Path, str]] = None) -> dict:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    cfg = {k: dict(v) for k, v in _DEFAULTS.items()}
    if p.exists():
        with open(p, encoding="utf-8") as f:
            user_cfg = toml.load(f)
        for section, values in user_cfg.items():
            if isinstance(values, dict):
                cfg.setdefault(section, {}).update(values)
            else:
                cfg[section] = values
    return cfg