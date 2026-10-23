import contextlib
import io

from hello import main


def test_prints_greeting():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main()
    assert out.getvalue() == "Looking goood feelin good :)\n"


if __name__ == "__main__":
    test_prints_greeting()
    print("PASS")
