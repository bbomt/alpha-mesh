import os
import subprocess
import sys
import unittest
from pathlib import Path

HELLO = Path(__file__).with_name("hello.py")


class TestHello(unittest.TestCase):
    def test_prints_owner_greeting(self):
        result = subprocess.run(
            [sys.executable, str(HELLO)],
            capture_output=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Hello Brave New World 🙂\n")


if __name__ == "__main__":
    unittest.main()
