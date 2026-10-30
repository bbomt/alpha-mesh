"""Every case file matches eval/schema/case.schema.json (PMAT-309 (b))."""

import json
from pathlib import Path

import jsonschema
import pytest

import eval_cases

SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "schema" / "case.schema.json").read_text())
CASES = eval_cases.case_files(eval_cases.CASES_DIR)


def test_the_dataset_is_not_empty():
    assert CASES


@pytest.mark.parametrize("path", CASES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_case_file_matches_the_schema(path):
    jsonschema.validate(json.loads(path.read_text()), SCHEMA)


@pytest.mark.parametrize("broken", [
    lambda c: c.pop("expected"),
    lambda c: c.pop("category"),
    lambda c: c.pop("notes"),
    lambda c: c.update(expected={"flag_file": "src/a.py"}),  # neither must_flag nor must_not_reraise
    lambda c: c.update(expected={**c["expected"], "must_mention": ["x"]}),  # unknown key
    lambda c: c.update(expected={**c["expected"], "min_severity": "critical"}),
], ids=["no expected", "no category", "no notes", "no must_ key", "unknown key", "bad severity"])
def test_the_schema_rejects_a_broken_case(broken):
    case = json.loads(CASES[0].read_text())
    broken(case)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(case, SCHEMA)
