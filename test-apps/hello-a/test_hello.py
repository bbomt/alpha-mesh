import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).with_name("hello.py")


def test_prints_greeting():
    out = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, check=True).stdout
    assert out == "Looking goood feelin good :)\n"


if __name__ == "__main__":
    test_prints_greeting()
    print("PASS")
