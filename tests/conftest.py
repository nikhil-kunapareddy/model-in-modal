import pathlib
import sys

# Make `backend` importable when running `pytest` from the repo root.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
