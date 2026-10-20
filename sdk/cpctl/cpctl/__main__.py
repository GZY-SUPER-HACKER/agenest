"""`python -m cpctl` entry point, so the SDK runs from a source checkout with no install."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
