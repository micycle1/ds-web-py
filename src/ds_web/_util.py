from __future__ import annotations

import os
import sys
import warnings

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))


def warn(message: str, category: type[Warning] = UserWarning) -> None:
    """warnings.warn, attributed to the first caller outside this package — so a warning
    raised deep inside the library points at the user's line, however it got there."""
    level = 1
    frame = sys._getframe(1)
    while frame is not None and os.path.abspath(frame.f_code.co_filename).startswith(_PACKAGE_DIR):
        frame = frame.f_back  # type: ignore[assignment]
        level += 1
    warnings.warn(message, category, stacklevel=level)
