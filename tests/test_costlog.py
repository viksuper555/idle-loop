"""Tests for the cost log: round-trip, measured prior, similarity, fail-soft."""

from __future__ import annotations

import os

import costlog
from models import EstimateFeatures, RunRecord


def _record(
    ticket_id: int = 1,
    *,
    features: dict | None = None,
    actual_cost: float = 10.0,
    actual_iterations: int = 5,
    outcome: str = "merged",
) -> RunRecord:
    return RunRecord(
        ticket_id=ticket_id,
        title=f"ticket {ticket_id}",
        features=features if features is not None else {"n_criteria": 3, "est_files": 2},
        estimated_cost=8.0,
        estimated_iterations=4.0,
        actual_cost=actual_cost,
        actual_iterations=actual_iterations,
        outcome=outcome,
    )


def test_append_read_round_trip(tmp_path):
    path = str(tmp_path / "cost_log.jsonl")
    r1 = _record(1)
    r2 = _record(2, actual_cost=20.0, actual_iterations=2)
    costlog.append_record(r1, path)
    costlog.append_record(r2, path)

    rows = costlog.read_all(path)
    assert len(rows) == 2
    assert [r.ticket_id for r in rows] == [1, 2]
    assert rows[0] == r1
    assert rows[1] == r2


def test_append_creates_parent_dirs(tmp_path):
    path = str(tmp_path / "nested" / "deep" / "cost_log.jsonl")
    costlog.append_record(_record(7), path)
    assert os.path.exists(path)
    assert len(costlog.read_all(path)) == 1


def test_read_all_missing_file_returns_empty(tmp_path):
    path = str(tmp_path / "does_not_exist.jsonl")
    assert costlog.read_all(path) == []


def test_read_all_skips_malformed_and_blank_lines(tmp_path):
    path = str(tmp_path / "cost_log.jsonl")
    costlog.append_record(_record(1), path)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("not json at all\n")
        fh.write("\n")
        fh.write('{"partial": true}\n')  # valid json, wrong shape -> skipped
    costlog.append_record(_record(2), path)

    rows = costlog.read_all(path)
    assert [r.ticket_id for r in rows] == [1, 2]


def test_measured_cost_per_iteration_math(tmp_path):
    path = str(tmp_path / "cost_log.jsonl")
    # 10/5 = 2.0 and 20/2 = 10.0 -> mean 6.0
    costlog.append_record(_record(1, actual_cost=10.0, actual_iterations=5), path)
    costlog.append_record(_record(2, actual_cost=20.0, actual_iterations=2), path)
    assert costlog.measured_cost_per_iteration(path) == 6.0


def test_measured_cost_per_iteration_ignores_zero_iteration_rows(tmp_path):
    path = str(tmp_path / "cost_log.jsonl")
    costlog.append_record(_record(1, actual_cost=10.0, actual_iterations=5), path)
    costlog.append_record(_record(2, actual_cost=99.0, actual_iterations=0), path)
    # Only the first row counts: 10/5 = 2.0
    assert costlog.measured_cost_per_iteration(path) == 2.0


def test_measured_cost_per_iteration_none_when_no_usable_rows(tmp_path):
    path = str(tmp_path / "cost_log.jsonl")
    assert costlog.measured_cost_per_iteration(path) is None  # missing file
    costlog.append_record(_record(1, actual_iterations=0), path)
    assert costlog.measured_cost_per_iteration(path) is None  # only zero rows


def test_most_similar_picks_nearest(tmp_path):
    path = str(tmp_path / "cost_log.jsonl")
    far = _record(
        1,
        features={"n_criteria": 9, "est_files": 9, "needs_tests": True, "ambiguity_score": 0.9},
    )
    near = _record(
        2,
        features={"n_criteria": 3, "est_files": 2, "needs_tests": False, "ambiguity_score": 0.1},
    )
    costlog.append_record(far, path)
    costlog.append_record(near, path)

    query = EstimateFeatures(
        n_criteria=3, est_files=2, needs_tests=False, ambiguity_score=0.1
    )
    best = costlog.most_similar(query, path)
    assert best is not None
    assert best.ticket_id == 2


def test_most_similar_handles_missing_feature_keys(tmp_path):
    path = str(tmp_path / "cost_log.jsonl")
    # Stored features are sparse; missing keys treated as zero.
    sparse = _record(1, features={"n_criteria": 0})
    populated = _record(
        2,
        features={"n_criteria": 5, "est_files": 5, "needs_tests": True, "ambiguity_score": 0.8},
    )
    costlog.append_record(sparse, path)
    costlog.append_record(populated, path)

    query = EstimateFeatures(
        n_criteria=0, est_files=0, needs_tests=False, ambiguity_score=0.0
    )
    best = costlog.most_similar(query, path)
    assert best is not None
    assert best.ticket_id == 1


def test_most_similar_none_when_empty(tmp_path):
    path = str(tmp_path / "cost_log.jsonl")
    query = EstimateFeatures(n_criteria=1)
    assert costlog.most_similar(query, path) is None  # missing file
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("garbage\n")
    assert costlog.most_similar(query, path) is None  # no valid rows
