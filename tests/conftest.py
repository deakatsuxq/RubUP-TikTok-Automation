"""Keep historical regressions importable without loading them in RubUP 4.0.

Only pytest adds the archived modules to its search path. The application's
entry point and the Windows executable never enable this path.
"""
from pathlib import Path
import sys

import gui


_archive = Path(__file__).resolve().parents[1] / 'legacy_v3'
sys.path.append(str(_archive))
gui.__path__.append(str(_archive / 'gui'))
