"""Promptfoo test generator (PMAT-304): `tests: file://promptfoo/load_cases.py:create_tests` (paths are relative to eval/promptfooconfig.yaml).

One test per development case in eval/cases/*/*.json, or per hold-out case when EVAL_HOLDOUT is set
(run_eval.sh --holdout), validated against case.schema.json (an invalid case stops the run). The case
travels whole in vars, so the provider and the asserts read the same one.

Development cases also get the judge (PMAT-311), so a missing JUDGE_* variable stops the run here,
before any review. The hold-out never reaches the judge: rubric iterations must not use it.
"""

import os

import llm_rubric
from case_rules import CASES_DIR, development_cases, holdout_cases

ASSERTS = [{"type": "python", "value": "file://promptfoo/asserts.py:schema_valid"},
           {"type": "python", "value": "file://promptfoo/asserts.py:case_expectations"}]
JUDGE = {"type": "python", "value": "file://promptfoo/llm_rubric.py:judge_results"}


def create_tests(config=None, cases_dir=CASES_DIR):
    if os.environ.get("EVAL_HOLDOUT"):
        cases, asserts = holdout_cases(cases_dir), ASSERTS
    else:
        llm_rubric.startup_check()
        cases, asserts = development_cases(cases_dir), [*ASSERTS, JUDGE]
    return [{"description": c["case_id"], "vars": {"case_id": c["case_id"], "case": c}, "assert": asserts,
             "metadata": {"category": c["category"], "action": c["expected"]["action"], "label": c["label"]}}
            for c in cases]
