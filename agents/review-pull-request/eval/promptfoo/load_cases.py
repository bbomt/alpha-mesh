"""Promptfoo test generator (PMAT-304): `tests: file://promptfoo/load_cases.py:create_tests` (paths are relative to eval/promptfooconfig.yaml).

One test per development case in eval/cases/*/*.json, or per hold-out case when EVAL_HOLDOUT is set
(run_eval.sh --holdout), validated against case.schema.json (an invalid case stops the run). The case
travels whole in vars, so the provider and the asserts read the same one. The asserts are in
promptfooconfig.yaml (defaultTest).

A development run also needs the judge (PMAT-311), so a missing JUDGE_* variable stops it here, before any review.
"""

import os

import llm_rubric
from case_rules import CASES_DIR, development_cases, holdout_cases


def create_tests(config=None, cases_dir=CASES_DIR):
    if os.environ.get("EVAL_HOLDOUT"):
        cases = holdout_cases(cases_dir)
    else:
        llm_rubric.startup_check()
        cases = development_cases(cases_dir)
    return [{"description": c["case_id"], "vars": {"case_id": c["case_id"], "case": c},
             "metadata": {"category": c["category"], "action": c["expected"]["action"], "label": c["label"]}}
            for c in cases]
