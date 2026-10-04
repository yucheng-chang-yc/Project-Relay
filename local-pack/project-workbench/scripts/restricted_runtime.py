"""Absolute, isolated-Python entrypoint for the task-specific Claude stdio adapter."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench.restricted import main
raise SystemExit(main())
