"""The error contract: one exception carries the exit code."""

from __future__ import annotations

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONTROL = 3
EXIT_PRECONDITION = 4
EXIT_CHECK = 5
EXIT_PROVENANCE = 6
EXIT_INTERNAL = 70


class MarginError(Exception):
    """A failure that maps to a documented exit code and a failure envelope."""

    def __init__(
        self,
        exit_code: int,
        message: str,
        *,
        month: str | None = None,
        controls: list[dict] | None = None,
        failed_controls: list[dict] | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message
        self.month = month
        self.controls = controls
        self.failed_controls = failed_controls
