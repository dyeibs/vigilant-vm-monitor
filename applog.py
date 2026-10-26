"""Shared logging setup (console + rotating file in ./logs)."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

import config


def setup_logging(mode: str) -> logging.Logger:
    root = logging.getLogger()
    if getattr(root, "_vigilant_configured", False):
        return logging.getLogger("vigilant")
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    try:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        fh = RotatingFileHandler(os.path.join(config.LOG_DIR, f"vigilant_{mode}.log"),
                                 maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as exc:  # read-only folder etc. — console logging still works
        root.warning("File logging disabled: %s", exc)
    root._vigilant_configured = True
    return logging.getLogger("vigilant")
