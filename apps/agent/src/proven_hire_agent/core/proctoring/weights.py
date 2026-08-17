"""Loads the SAME physical weight-table file the TS client reads.

Fixes the reference implementation's own footgun: it kept its weight table
in two hand-synced places (a TS constant AND a duplicated SQL CASE
statement), with a comment warning both must be kept in sync manually. Here
there is exactly one authored file — ``packages/shared/data/
proctoring-weights.json`` — and both languages read it directly (TS via a
native JSON import at build time, Python via this loader at runtime).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict

# apps/agent/src/proven_hire_agent/core/proctoring/weights.py -> repo root is
# 6 parents up. Same relative-path pattern apps/agent/tests/test_parity.py
# uses to reach packages/shared/schema/ from its own location.
_WEIGHTS_FILE = (
    Path(__file__).resolve().parents[6] / "packages" / "shared" / "data" / "proctoring-weights.json"
)


class ProctoringWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weights: dict[str, float]
    decay_per_step: float
    decay_step_sec: float
    strike_threshold_default: float

    def weight_for(self, event_type: str) -> float:
        """Any type not in the table is logged-only (weight 0) — never raises."""
        return self.weights.get(event_type, 0.0)


@lru_cache
def load_proctoring_weights() -> ProctoringWeights:
    data = json.loads(_WEIGHTS_FILE.read_text(encoding="utf-8"))
    return ProctoringWeights(**data)
