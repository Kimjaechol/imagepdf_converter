#!/usr/bin/env python3
"""Entry point to start the backend API server."""

import os
import sys

# Ensure the project root is in the Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.server import main

if __name__ == "__main__":
    main()
