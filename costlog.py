"""Append-only cost log for idle-loop.

Each run appends one :class:`~models.RunRecord` as a JSON line to
``cost_log.jsonl``. The log is both an audit trail and the training data /
measured prior the estimator reads back:

* :func:`measured_cost_per_iteration` — the mean observed dollars-per-iteration,
  used to anchor the cost estimate instead of a hard-coded constant.
* :func:`most_similar` — the nearest past ticket by a normalized euclidean
  distance over the cheap features, used to seed ``EstimateFeatures.similarity_cost``.

stdlib only. Reads fail soft: a missing file or a malformed line never raises.
"""

from __future__ import annotations

import math
import os

from models import EstimateFeatures, RunRecord

# Feature keys compared by ``most_similar`` and the rough scale of each, used to
# normalize the per-axis difference so no single axis dominates the distance.
_FEATURE_KEYS = ("n_criteria", "est_files", "needs_tests", "ambiguity_score")
_FEATURE_SCALE: dict[str, float] = {
    "n_criteria": 10.0,
    "est_files": 10.0,
    "needs_tests": 1.0,
    "ambiguity_score": 1.0,
}


def append_record(record: RunRecord, path: str) -> None:
    """Append ``record`` as a JSON line, creating the file and parents if needed."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(record.to_json() + "\n")


def read_all(path: str) -> list[RunRecord]:
    """Read every record. ``[]`` if the file is missing; malformed lines skipped."""
    if not os.path.exists(path):
        return []
    records: list[RunRecord] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(RunRecord.from_json(line))
            except Exception:
                # Defensive: a partial write or hand-edit must not break reads.
                continue
    return records


def measured_cost_per_iteration(path: str) -> float | None:
    """Mean ``actual_cost / actual_iterations`` over usable rows, else ``None``.

    Only rows with ``actual_iterations > 0`` count. Returns ``None`` when the log
    has no such rows, signalling the estimator to fall back to its config prior.
    """
    ratios = [
        rec.actual_cost / rec.actual_iterations
        for rec in read_all(path)
        if rec.actual_iterations > 0
    ]
    if not ratios:
        return None
    return sum(ratios) / len(ratios)


def _feature_value(features: dict, key: str) -> float:
    """Coerce ``features[key]`` to a float, treating bools/missing keys safely."""
    value = features.get(key, 0)
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _distance(a: dict, b: dict) -> float:
    """Normalized euclidean distance over ``_FEATURE_KEYS``."""
    total = 0.0
    for key in _FEATURE_KEYS:
        scale = _FEATURE_SCALE[key]
        diff = (_feature_value(a, key) - _feature_value(b, key)) / scale
        total += diff * diff
    return math.sqrt(total)


def most_similar(features: EstimateFeatures, path: str) -> RunRecord | None:
    """Return the past record whose stored features are closest to ``features``.

    Distance is a simple normalized euclidean over ``(n_criteria, est_files,
    needs_tests, ambiguity_score)``; missing keys count as zero. ``None`` when the
    log has no rows.
    """
    records = read_all(path)
    if not records:
        return None
    target = features.to_dict()
    return min(records, key=lambda rec: _distance(target, rec.features or {}))
