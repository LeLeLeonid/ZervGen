import sys
from pathlib import Path

# Add src to path so imports work cleanly
sys.path.append(str(Path(__file__).parent))

from src.cli import main

if __name__ == "__main__":
    main()