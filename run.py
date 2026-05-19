#!/usr/bin/env python3
"""Top-level CLI entry point. Usage: python run.py [--config ...]"""
import sys

from src.main import main

if __name__ == "__main__":
    sys.exit(main())
