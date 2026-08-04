"""Exit codes and the exception hierarchy shared by every vidprep module.

Exit codes follow design.md §6: ``0`` success, ``1`` usage/environment error,
``2`` failure while executing a stage, ``3`` verification failure (invalid
schema, hash mismatch).
"""

from __future__ import annotations

from typing import ClassVar

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


class HashMismatchError(VidprepError):
    """The source material no longer matches the sha256 recorded at init."""

    exit_code: ClassVar[int] = EXIT_VALIDATION
    code: ClassVar[str] = "hash_mismatch"
