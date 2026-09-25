#!/usr/bin/env python3
"""Corta Clips: arranca con `python3 app.py` o con doble clic en iniciar.command."""

import sys

if sys.version_info < (3, 10):
    sys.exit("Corta Clips necesita Python 3.10 o superior. En Mac: brew install python")

from cortaclips.server import main

if __name__ == "__main__":
    main()
