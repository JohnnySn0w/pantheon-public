"""The run transcript: typed JSON events (ORG-080 §4 "Transcript + receipts", D-R).

Events only, never receipts and never secrets (ORG-050): each line is
``{"schema", "seq", "correlation_id", "run_id", "event", ...fields}``. A run's
transcript is appended to ``transcript.jsonl`` in its run directory; a
read-only ``plan`` (no run, T-5074 bubble 1) only streams events to stdout.
"""

from __future__ import annotations

import json
from typing import Any, Callable, TextIO

from tools.germinate.engine.run_state import RunHandle, now_iso

SCHEMA = "pantheon-germinate-transcript/v0"


class Transcript:
    def __init__(
        self,
        correlation_id: str,
        *,
        echo: TextIO | None = None,
        sink: Callable[[bytes], None] | None = None,
        run_id: str | None = None,
    ) -> None:
        self.correlation_id = correlation_id
        self.run_id = run_id
        self._echo = echo
        self._sink = sink
        self._seq = 0
        self.events: list[dict[str, Any]] = []

    def bind(self, handle: RunHandle) -> "Transcript":
        """A per-run child stream sharing this correlation id."""

        return Transcript(self.correlation_id, echo=self._echo, sink=handle.append_transcript, run_id=handle.tuple.run_id)

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        self._seq += 1
        entry = {
            "schema": SCHEMA,
            "seq": self._seq,
            "at": now_iso(),
            "correlation_id": self.correlation_id,
            "run_id": self.run_id,
            "event": event,
            **fields,
        }
        line = (json.dumps(entry, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
        if self._sink is not None:
            self._sink(line)
        if self._echo is not None:
            self._echo.write(line.decode("utf-8"))
        self.events.append(entry)
        return entry
