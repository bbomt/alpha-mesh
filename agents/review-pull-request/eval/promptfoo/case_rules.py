"""What a case expects of a review, checked on the review's findings (PMAT-304).

A case is reviewed at raised_sha, the commit its finding was raised on, so the code is the same and
the finding's line is a stable anchor: a finding "is" the case's finding when it is in flag_file
within ±WINDOW lines of it. One place for the rules: the Promptfoo asserts, the baseline and the
compare script all call check().
"""

import json
import re
from pathlib import Path

import jsonschema

EVAL = Path(__file__).resolve().parent.parent
CASES_DIR = EVAL / "cases"
HOLDOUT = EVAL / "holdout.json"
CASE_SCHEMA = json.loads((EVAL / "schema" / "case.schema.json").read_text())
REVIEW_SCHEMA = json.loads((EVAL / "schema" / "review.schema.json").read_text())

WINDOW = 3  # lines either side of the case's finding (owner, 2026-10-30)
RANK = {"low": 0, "medium": 1, "high": 2}


def glob_regex(glob: str) -> re.Pattern:
    """git / minimatch rules on a whole repo-relative path: * and ? stay within one folder, ** spans any depth.
    (fnmatch lets * cross /, so it cannot be used.)"""
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif glob.startswith("**", i):
            out, i = out + ".*", i + 2
        elif glob[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif glob[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(glob[i]), i + 1
    return re.compile(out + r"\Z")


def case_files(cases_dir: Path) -> list[Path]:
    return sorted(Path(cases_dir).glob("*/*.json"))


def load_cases(cases_dir: Path = CASES_DIR) -> list[dict]:
    """Every case, each validated against case.schema.json; an invalid one is refused, naming its file."""
    cases = []
    for path in case_files(cases_dir):
        case = json.loads(path.read_text())
        try:
            jsonschema.validate(case, CASE_SCHEMA)
        except jsonschema.ValidationError as e:
            raise ValueError(f"{path}: invalid case: {e.message}") from e
        cases.append(case)
    return cases


def holdout_ids() -> set[str]:
    return set(json.loads(HOLDOUT.read_text())["cases"])


def development_cases(cases_dir: Path | None = None) -> list[dict]:
    """Every case outside the hold-out: what normal runs, the baseline and the comparison use."""
    held = holdout_ids()
    return [c for c in load_cases(cases_dir or CASES_DIR) if c["case_id"] not in held]


def holdout_cases(cases_dir: Path | None = None) -> list[dict]:
    held = holdout_ids()
    return [c for c in load_cases(cases_dir or CASES_DIR) if c["case_id"] in held]


def _near(finding: dict, flag_file: str, line: int, window: int) -> bool:
    return finding["file"] == flag_file and finding["line"] is not None and abs(finding["line"] - line) <= window


def check(case: dict, review: dict, window: int = WINDOW) -> tuple[bool, str]:
    """(passed, reason) for the case's expected action on the review's findings."""
    exp, findings = case["expected"], review["findings"]
    action, flag_file = exp["action"], exp["flag_file"]
    if action == "raise_true_bug":
        line, minimum, mention = case["finding"]["line"], exp["min_severity"], exp.get("must_mention_any")
        hits = [f for f in findings if _near(f, flag_file, line, window) and RANK[f["severity"]] >= RANK[minimum]]
        if mention:
            hits = [f for f in hits
                    if any(m.lower() in f"{f['summary']} {f.get('suggestion') or ''}".lower() for m in mention)]
        if hits:
            return True, f"raise_true_bug: raised in {flag_file} at line {hits[0]['line']}"
        return False, (f"raise_true_bug: no finding in {flag_file} within ±{window} lines of line {line} "
                       f"at {minimum} or above" + (f" mentioning any of {mention}" if mention else ""))
    if action == "skip_false_bug":
        line = case["finding"]["line"]
        hit = next((f for f in findings if _near(f, flag_file, line, window)), None)
        if hit:
            return False, (f"skip_false_bug: re-raised in {flag_file} at line {hit['line']} "
                           f"(within ±{window} lines of line {line}): {hit['summary'][:200]}")
        return True, f"skip_false_bug: not re-raised in {flag_file} near line {line}"
    pattern, minimum, limit = glob_regex(flag_file), exp.get("min_severity"), exp.get("max_comments")
    matching = [f for f in findings if pattern.match(f["file"])]
    hit = next((f for f in matching if minimum is None or RANK[f["severity"]] >= RANK[minimum]), None)
    if hit:
        return False, (f"raise_nothing: finding in {hit['file']} at line {hit['line']} ({hit['severity']}), "
                       f"which matches {flag_file}")
    if limit is not None and len(matching) > limit:
        return False, f"raise_nothing: {len(matching)} findings match {flag_file}, more than the {limit} allowed"
    return True, f"raise_nothing: no finding in {flag_file}"


def check_file_level(case: dict, review: dict) -> bool:
    """The file-only rule of PMAT-309, kept in the baseline for comparison: any finding in flag_file
    (at min_severity or above for a true bug) counts as the case's finding."""
    exp = case["expected"]
    if exp["action"] == "raise_nothing":
        return check(case, review)[0]
    in_file = [f for f in review["findings"] if f["file"] == exp["flag_file"]]
    if exp["action"] == "raise_true_bug":
        return any(RANK[f["severity"]] >= RANK[exp["min_severity"]] for f in in_file)
    return not in_file
