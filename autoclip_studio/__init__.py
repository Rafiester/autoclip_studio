"""AutoClip Studio application package."""
from __future__ import annotations

import os
import sys

# On macOS, default to Qt's native Apple AVFoundation ("darwin") multimedia backend before
# QtMultimedia loads so 48 kHz AAC clips play audibly on 44.1 kHz-capped Bluetooth devices.
if sys.platform == "darwin":
    os.environ.setdefault("QT_MEDIA_BACKEND", "darwin")

__version__ = "1.0.0"

