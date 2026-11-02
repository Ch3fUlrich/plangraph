"""Make the repository root importable so ``tools.*`` imports work under a
plain ``pytest`` run (only ``python3 -m pytest`` puts the cwd on sys.path).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
