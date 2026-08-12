"""Shell-free subprocess runner for typed Pantheon harness adapters."""

from __future__ import annotations

import dataclasses
import os
import subprocess
import threading
from collections.abc import Callable

from tools.harness_adapter.base import PantheonHarnessAdapter
from tools.harness_adapter.types import (
    ExecutionError,
    ExecutionRequest,
    ExecutionResult,
    HarnessEvent,
)


class HarnessRunner:
    """Execute one headless harness turn and preserve structural events."""

    def run(
        self,
        adapter: PantheonHarnessAdapter,
        request: ExecutionRequest,
        *,
        on_event: Callable[[HarnessEvent], None] | None = None,
    ) -> ExecutionResult:
        spec = (
            adapter.build_resume(
                adapter.session_handle(request.session_id, request.cwd),
                request.prompt,
            )
            if request.session_id
            else adapter.build_exec(request)
        )
        events: list[HarnessEvent] = []
        stderr_parts: list[str] = []
        callback_error: Exception | None = None
        env = os.environ.copy()
        env.update(spec.env)

        try:
            proc = subprocess.Popen(
                spec.argv,
                cwd=spec.cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                bufsize=1,
            )
        except OSError as exc:
            event = adapter.spawn_error_event(exc)
            result = adapter.finalize([event], returncode=127, stderr=str(exc))
            return dataclasses.replace(result, command_argv=spec.argv)

        def _drain_stderr() -> None:
            assert proc.stderr is not None
            stderr_parts.extend(proc.stderr.readlines())

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        stdin_error = ""
        assert proc.stdin is not None
        try:
            if spec.stdin is not None:
                proc.stdin.write(spec.stdin)
        except (BrokenPipeError, OSError) as exc:
            stdin_error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError) as exc:
                if not stdin_error:
                    stdin_error = f"{type(exc).__name__}: {exc}"

        assert proc.stdout is not None
        for line in proc.stdout:
            event = adapter.parse_event(line)
            if event is None:
                continue
            events.append(event)
            if on_event is not None:
                try:
                    on_event(event)
                except Exception as exc:  # callback is a local evidence boundary
                    callback_error = exc
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                    break

        returncode = proc.wait()
        stderr_thread.join(timeout=5)
        stderr = "".join(stderr_parts)
        if stdin_error:
            stderr = f"{stdin_error}\n{stderr}".rstrip()
        if callback_error is not None:
            message = (
                "progress callback failed: "
                f"{type(callback_error).__name__}: {callback_error}"
            )
            return ExecutionResult(
                harness=adapter.HARNESS_NAME,
                status="failed",
                final_message=None,
                session=None,
                usage=None,
                model=request.model,
                returncode=returncode,
                error=ExecutionError(
                    category="config",
                    message=message,
                    retryable=True,
                    raw={"callback_error": type(callback_error).__name__},
                ),
                command_argv=spec.argv,
            )
        result = adapter.finalize(
            events,
            returncode=returncode,
            stderr=stderr,
        )
        return dataclasses.replace(result, command_argv=spec.argv)
