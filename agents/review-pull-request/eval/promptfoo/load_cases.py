"""Promptfoo test generator (PMAT-304): `tests: file://promptfoo/load_cases.py:create_tests` (paths are relative to eval/promptfooconfig.yaml).

One test per development case in eval/cases/*/*.json, or per hold-out case when EVAL_HOLDOUT is set
(run_eval.sh --holdout), validated against case.schema.json (an invalid case stops the run). The case
travels whole in vars, so the provider and the asserts read the same one.
"""

import os

from case_rules import CASES_DIR, development_cases, holdout_cases

ASSERTS = [{"type": "python", "value": "file://promptfoo/asserts.py:schema_valid"},
           {"type": "python", "value": "file://promptfoo/asserts.py:case_expectations"}]


def create_tests(config=None, cases_dir=CASES_DIR):
    cases = holdout_cases(cases_dir) if os.environ.get("EVAL_HOLDOUT") else development_cases(cases_dir)
    return [{"description": c["case_id"], "vars": {"case_id": c["case_id"], "case": c}, "assert": ASSERTS,
             "metadata": {"category": c["category"], "action": c["expected"]["action"], "label": c["label"]}}
            for c in cases]
