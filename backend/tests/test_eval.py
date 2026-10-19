"""Runs the dialogue eval suite as part of CI (ТЗ §15 acceptance)."""
from __future__ import annotations

import pytest

from arix.eval.cases import CASES
from arix.eval.runner import THRESHOLDS, run_all

from .conftest import requires_db

pytestmark = [requires_db, pytest.mark.slow]


async def test_eval_suite_meets_the_acceptance_thresholds(clean_db):
    report = await run_all()
    metrics = report["metrics"]
    failed = [case for case in report["cases"] if not case["passed"]]
    detail = "\n".join(f"{c['id']}: {', '.join(c['failures'])}" for c in failed)

    assert metrics["invented_prices"] == THRESHOLDS["invented_prices"], detail
    assert metrics["diagnoses"] == THRESHOLDS["diagnoses"], detail
    assert metrics["red_flag_handoff_rate"] >= THRESHOLDS["red_flag_handoff_rate"], detail
    assert metrics["booking_success_rate"] >= THRESHOLDS["booking_success_rate"], detail
    assert metrics["overall_pass_rate"] >= THRESHOLDS["overall_pass_rate"], detail


def test_the_suite_covers_every_required_category():
    categories = {case.category for case in CASES}
    # ТЗ §15 names these explicitly.
    required = {
        "price", "booking", "reschedule", "red_flag", "diagnosis",
        "discount", "rude", "injection", "faq",
    }
    assert required <= categories
    assert len(CASES) >= 60
    assert {case.lang for case in CASES} == {"ru", "kz", "uz"}
