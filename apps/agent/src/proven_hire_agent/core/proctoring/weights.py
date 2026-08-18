"""Loads the SAME physical weight-table file the TS client reads.

Fixes the reference implementation's own footgun: it kept its weight table
in two hand-synced places (a TS constant AND a duplicated SQL CASE
statement), with a comment warning both must be kept in sync manually. Here
there is exactly one authored file — ``apps/agent/data/
proctoring-weights.json`` — and both languages read it directly (Python via
this loader at runtime, TS via a native JSON import reaching across at
build time, ``packages/shared/src/proctoring.ts``).

Deliberately lives under ``apps/agent/``, NOT ``packages/shared/``: every
Docker image this project actually builds for a Python service (Render's
agent-api via ``Dockerfile.api``, the LiveKit Cloud worker via
``Dockerfile``) uses ``apps/agent`` as its OWN build context — the wider
monorepo, including ``packages/shared``, is never copied in. A file that
needs to exist inside those images has to live somewhere within
``apps/agent`` itself; the JSON path below is relative to THIS file's own
location for exactly that reason, not to the repo root, so it resolves
correctly both in local dev (full checkout) and inside a container that
only has ``apps/agent``'s tree (``WORKDIR /app`` mirrors ``apps/agent``'s
own root in every such image — see the ``COPY data ./data`` lines in
``Dockerfile``/``Dockerfile.api``).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict

# apps/agent/src/proven_hire_agent/core/proctoring/weights.py -> apps/agent
# (this service's own root, present in every deployment context) is 4
# parents up.
_WEIGHTS_FILE = Path(__file__).resolve().parents[4] / "data" / "proctoring-weights.json"


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
