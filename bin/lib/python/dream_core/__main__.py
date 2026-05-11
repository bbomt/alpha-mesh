"""Entry point for: python3 -m dream_core"""

import sys
import os

# Add lib/python to path so dream_core can be imported as a package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dream_core import main

if __name__ == "__main__":
    sys.exit(main())