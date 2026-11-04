"""Promptfoo python asserts (PMAT-304): `value: file://asserts.py:<function>`, called as fn(output, context)."""

import json

import jsonschema

from case_rules import REVIEW_SCHEMA, check


def _review(output):
    return json.loads(output) if isinstance(output, str) else output


def schema_valid(output, context):
    try:
        jsonschema.validate(_review(output), REVIEW_SCHEMA)
    except (jsonschema.ValidationError, json.JSONDecodeError) as e:
        return {"pass": False, "score": 0, "reason": f"schema_valid: {e}"}
    return {"pass": True, "score": 1, "reason": "schema_valid: the review matches review.schema.json"}


def case_expectations(output, context):
    passed, reason = check(context["vars"]["case"], _review(output))
    return {"pass": passed, "score": 1 if passed else 0, "reason": reason}
