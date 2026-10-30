"""Promptfoo test generator (PMAT-304): `tests: file://promptfoo/load_cases.py:create_tests` (paths are relative to eval/promptfooconfig.yaml).

One test per case in eval/cases/*/*.json, validated against case.schema.json (an invalid case stops
the run). The case travels whole in vars, so the provider and the asserts read the same one.
"""

from case_rules import CASES_DIR, load_cases

ASSERTS = [{"type": "python", "value": "file://promptfoo/asserts.py:schema_valid"},
           {"type": "python", "value": "file://promptfoo/asserts.py:case_expectations"}]


def create_tests(config=None, cases_dir=CASES_DIR):
    return [{"description": c["case_id"], "vars": {"case_id": c["case_id"], "case": c}, "assert": ASSERTS,
             "metadata": {"category": c["category"], "action": c["expected"]["action"], "label": c["label"]}}
            for c in load_cases(cases_dir)]
