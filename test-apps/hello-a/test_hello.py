import contextlib
import io
import runpy
from pathlib import Path

SCRIPT = Path(__file__).with_name("hello.py")


def test_prints_greeting():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert out.getvalue() == "Looking goood feelin good :)\n"


if __name__ == "__main__":
    test_prints_greeting()
    print("PASS")
