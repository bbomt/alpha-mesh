import io
import runpy
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HELLO = Path(__file__).with_name("hello.py")


class TestHello(unittest.TestCase):
    def test_prints_owner_greeting(self):
        out = io.StringIO()
        with redirect_stdout(out):
            runpy.run_path(str(HELLO), run_name="__main__")
        self.assertEqual(out.getvalue(), "Hello Brave New World 🙂\n")


if __name__ == "__main__":
    unittest.main()
