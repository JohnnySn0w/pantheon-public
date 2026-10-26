"""PMB-1 v1: the acceptance harness for the ORG-062 memory organ (T-2518 step 1).

A benchmark a memory substrate CAN FAIL. Normative source: the agent-memory
evidence synthesis report section 6, ratified as the ORG-062 gate. v1 gates
tier 1 (episodic retrieval): Track A1 + A-retrieve + E3, plus the controls the
spec makes non-negotiable. Synthetic data only; no cloud model calls.
"""
from __future__ import annotations

from .gate import Criterion, Verdict, gate

__all__ = ["Criterion", "Verdict", "gate"]
