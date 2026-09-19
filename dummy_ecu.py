#!/usr/bin/env python3
"""Start the Dummy ECU: its window, or the command-line version with --console (--help lists the options)."""
import sys

from canexpert.simulator.ecu import main

if __name__ == "__main__":
    sys.exit(main())
