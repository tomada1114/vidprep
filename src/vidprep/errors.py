"""Exit codes and the exception hierarchy shared by every vidprep module.

Exit codes follow design.md §6: ``0`` success, ``1`` usage/environment error,
``2`` failure while executing a stage, ``3`` verification failure (invalid
schema, hash mismatch).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from collections.abc import Sequence

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_EXECUTION = 2
EXIT_VALIDATION = 3


class VidprepError(Exception):
    """Base class for every error vidprep reports to its user.

    Attributes:
        exit_code: Process exit code the CLI uses for this error.
        code: Stable machine-readable identifier emitted with ``--json``.
    """

    exit_code: ClassVar[int] = EXIT_EXECUTION
    code: ClassVar[str] = "error"

    def payload(self) -> dict[str, Any]:
        """Render the failure as the JSON object ``--json`` prints."""
        return {"error": self.code, "detail": str(self)}


class UsageError(VidprepError):
    """The command was invoked wrongly or the environment is not ready."""

    exit_code: ClassVar[int] = EXIT_USAGE
    code: ClassVar[str] = "usage"


class NotAProjectError(UsageError):
    """The target directory does not contain a ``vidprep.json`` manifest."""

    code: ClassVar[str] = "not_a_project"


class StageNotImplementedError(UsageError):
    """The subcommand exists as a skeleton but its stage is not built yet."""

    code: ClassVar[str] = "not_implemented"


class ExecutionFailedError(VidprepError):
    """A stage started but could not complete."""

    exit_code: ClassVar[int] = EXIT_EXECUTION
    code: ClassVar[str] = "exec_failed"


class FfmpegError(ExecutionFailedError):
    """An ffmpeg or ffprobe invocation exited non-zero."""

    code: ClassVar[str] = "ffmpeg_failed"


class AsrFailedError(ExecutionFailedError):
    """The ASR backend exited non-zero, or left output vidprep cannot read.

    Kept apart from :class:`FfmpegError` so a machine reading ``--json`` can
    tell "the recogniser broke" from "a media command broke"; both stop the
    stage before anything is written (design.md §6).
    """

    code: ClassVar[str] = "asr_failed"


class TimelineSchemaError(ExecutionFailedError):
    """auto-editor exported a timeline in a shape vidprep does not know.

    A conversion layer that guessed would move cut boundaries without saying
    so, which is the one failure nobody would notice, so an unexpected
    ``--export v3`` document stops the stage instead (design.md §5.4).
    """

    code: ClassVar[str] = "timeline_schema"


class SchemaInvalidError(VidprepError):
    """A JSON artifact violated its schema or an invariant."""

    exit_code: ClassVar[int] = EXIT_VALIDATION
    code: ClassVar[str] = "schema_invalid"


class InvariantViolationError(VidprepError):
    """A stage produced output that breaks an invariant the design guarantees.

    The work is thrown away rather than published, so this is a verification
    failure (exit ``3``) and not an execution failure.
    """

    exit_code: ClassVar[int] = EXIT_VALIDATION
    code: ClassVar[str] = "invariant_violated"


class PatchInvalidError(VidprepError):
    """A correction patch failed the checks made before anything is applied.

    Every problem found in the patch is reported at once, because a patch is
    written by a language model and fixing one complaint at a time would mean
    another round trip per mistake. Nothing is written when this is raised, so
    the report states plainly that no segment was touched (design.md §5.3).
    """

    exit_code: ClassVar[int] = EXIT_VALIDATION
    code: ClassVar[str] = "patch_invalid"

    def __init__(self, details: Sequence[str]) -> None:
        """Record every complaint *details* raises against the patch."""
        super().__init__("; ".join(details))
        self.details = list(details)

    def payload(self) -> dict[str, Any]:
        """Render the failure with every complaint and the untouched count."""
        return {"error": self.code, "detail": self.details, "applied": 0}


class HashMismatchError(VidprepError):
    """The source material no longer matches the sha256 recorded at init."""

    exit_code: ClassVar[int] = EXIT_VALIDATION
    code: ClassVar[str] = "hash_mismatch"
