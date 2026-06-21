"""Make the repo root importable so `import bizconnect` works under pytest
regardless of how pytest is invoked."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
