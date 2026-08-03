"""Creation, loading and bookkeeping of a vidprep project directory.

A project is one video: ``vidprep.json`` (manifest) plus ``profile.json``
(parameters) plus whatever artifacts the stages have produced. Everything a
stage writes goes through :func:`write_json`, which never leaves a half-written
file behind, and the source material itself is only ever read.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from . import _ffmpeg
from .errors import (
    HashMismatchError,
    NotAProjectError,
    SchemaInvalidError,
    UsageError,
)
from .models import (
    Cuts,
    Manifest,
    Profile,
    Source,
    StageRecord,
    Styles,
    Telops,
    Transcript,
    describe_validation_error,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import BaseModel

MANIFEST_NAME = "vidprep.json"
PROFILE_NAME = "profile.json"
SOURCE_DIR = "source"
HASH_CHUNK_BYTES = 1024 * 1024

#: Artifacts validated before a stage runs, in the order they are reported.
ARTIFACT_MODELS: Mapping[str, type[BaseModel]] = {
    "transcript.json": Transcript,
    "cuts.json": Cuts,
    "telops.json": Telops,
    "styles.json": Styles,
}

#: Profile sections whose values change a stage's output (used for provenance).
STAGE_PROFILE_SECTIONS: Mapping[str, tuple[str, ...]] = {
    "audio_fix": ("audio",),
    "transcribe": (),
    "correct": (),
    "detect": ("silence", "filler"),
    "render": ("render", "subtitle"),
    "report": (),
}

#: Stages whose output a given stage consumes, checked for staleness.
STAGE_UPSTREAM: Mapping[str, tuple[str, ...]] = {
    "audio_fix": (),
    "transcribe": ("audio_fix",),
    "correct": ("transcribe",),
    "detect": ("audio_fix", "transcribe"),
    "render": ("audio_fix", "transcribe", "detect"),
    "report": ("detect", "render"),
}


@dataclass(frozen=True, slots=True)
class Project:
    """A loaded project directory."""

    root: Path
    manifest: Manifest
    profile: Profile

    @property
    def source_path(self) -> Path:
        """Absolute path of the source material, resolved against the project."""
        path = Path(self.manifest.source.path)
        return path if path.is_absolute() else self.root / path


def sha256_file(path: Path) -> str:
    """Return the hex sha256 digest of *path*, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    """Write *text* to *path* atomically.

    The content lands in a sibling temporary file first, so a failure anywhere
    before :func:`os.replace` leaves any previous version of *path* untouched
    (design.md §6).
    """
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)  # noqa: PTH105 — pathlib has no atomic replace
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, model: BaseModel) -> None:
    """Serialise *model* to *path* as pretty-printed JSON, atomically."""
    payload = model.model_dump(mode="json")
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def default_profile() -> Profile:
    """Return the packaged default profile (design.md §3.6)."""
    template = resources.files(__package__).joinpath("profiles/default.json")
    return Profile.model_validate_json(template.read_text(encoding="utf-8"))


def _load_model[ModelT: BaseModel](
    path: Path,
    model: type[ModelT],
    duration: float | None = None,
) -> ModelT:
    """Parse *path* into *model*.

    Raises:
        SchemaInvalidError: If the file is not JSON or violates the schema.
    """
    context = None if duration is None else {"duration": duration}
    try:
        # Bytes, not text: pydantic reports undecodable input as invalid JSON
        # rather than letting a UnicodeDecodeError escape as a crash.
        return model.model_validate_json(path.read_bytes(), context=context)
    except ValidationError as exc:  # also raised for malformed JSON
        msg = f"{path.name}: {describe_validation_error(exc)}"
        raise SchemaInvalidError(msg) from exc


def init_project(
    directory: Path,
    source: Path,
    *,
    copy_source: bool = False,
) -> Project:
    """Create a project directory for *source* and write its manifest.

    Args:
        directory: Directory to create; it must not already hold files.
        source: Video file to process. It is read, never written.
        copy_source: Import the material into ``<project>/source/`` instead of
            referencing it by absolute path.

    Returns:
        The freshly created project.

    Raises:
        UsageError: If *source* is missing or *directory* exists and is not empty.
    """
    source = source.expanduser().resolve()
    if not source.is_file():
        msg = f"source material not found: {source}"
        raise UsageError(msg)
    directory = directory.expanduser().resolve()
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        msg = f"{directory} already exists and is not an empty directory"
        raise UsageError(msg)

    probed = _ffmpeg.probe(source)
    digest = sha256_file(source)
    directory.mkdir(parents=True, exist_ok=True)

    recorded_path = str(source)
    if copy_source:
        imported = directory / SOURCE_DIR / source.name
        imported.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, imported)
        recorded_path = str(imported.relative_to(directory))

    manifest = Manifest(
        created_at=datetime.now(tz=UTC).astimezone(),
        source=Source(
            path=recorded_path,
            sha256=digest,
            duration=probed.duration,
            video=probed.video,
            audio=probed.audio,
        ),
    )
    profile = default_profile()
    write_json(directory / MANIFEST_NAME, manifest)
    write_json(directory / PROFILE_NAME, profile)
    return Project(root=directory, manifest=manifest, profile=profile)


def init_plan(
    directory: Path, source: Path, *, copy_source: bool = False
) -> dict[str, Any]:
    """Return what :func:`init_project` would run and write, without doing it."""
    directory = directory.expanduser().resolve()
    source = source.expanduser().resolve()
    writes = [str(directory / MANIFEST_NAME), str(directory / PROFILE_NAME)]
    if copy_source:
        writes.append(str(directory / SOURCE_DIR / source.name))
    return {
        "action": "init",
        "project": str(directory),
        "commands": [_ffmpeg.probe_command(source)],
        "writes": writes,
    }


def load_project(directory: Path | None = None) -> Project:
    """Load the project rooted at *directory* (the current directory by default).

    Raises:
        NotAProjectError: If the directory holds no manifest.
        UsageError: If the manifest is there but ``profile.json`` is missing.
        SchemaInvalidError: If either file violates its schema.
    """
    root = (directory or Path.cwd()).expanduser().resolve()
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        msg = f"{root} is not a vidprep project ({MANIFEST_NAME} not found)"
        raise NotAProjectError(msg)
    profile_path = root / PROFILE_NAME
    if not profile_path.is_file():
        msg = f"{root} is missing {PROFILE_NAME}"
        raise UsageError(msg)
    manifest = _load_model(manifest_path, Manifest)
    profile = _load_model(profile_path, Profile)
    return Project(root=root, manifest=manifest, profile=profile)


def verify_source(project: Project) -> None:
    """Re-hash the source material and compare it with the manifest.

    Raises:
        UsageError: If the material has moved away or been deleted.
        HashMismatchError: If the material was replaced since ``init``.
    """
    path = project.source_path
    if not path.is_file():
        msg = f"source material not found: {path}"
        raise UsageError(msg)
    actual = sha256_file(path)
    expected = project.manifest.source.sha256
    if actual != expected:
        msg = f"source sha256 mismatch for {path}: expected {expected}, actual {actual}"
        raise HashMismatchError(msg)


def validate_artifacts(project: Project) -> list[str]:
    """Validate every artifact present in the project against its schema.

    Returns:
        The names of the artifacts that were checked.

    Raises:
        SchemaInvalidError: On the first artifact that fails validation.
    """
    checked = []
    for name, model in ARTIFACT_MODELS.items():
        path = project.root / name
        if path.is_file():
            _load_model(path, model, duration=project.manifest.source.duration)
            checked.append(name)
    return checked


def stage_params_sha256(profile: Profile, stage: str) -> str:
    """Hash the profile sections that determine *stage*'s output."""
    dumped = profile.model_dump(mode="json")
    payload = {name: dumped[name] for name in STAGE_PROFILE_SECTIONS.get(stage, ())}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def stale_upstream_warnings(project: Project, stage: str) -> list[str]:
    """Return warnings for upstream artifacts built with a different profile.

    Running on stale inputs is allowed on purpose (design.md §3.2): the user is
    told, not blocked.
    """
    warnings = []
    for upstream in STAGE_UPSTREAM.get(stage, ()):
        record = project.manifest.stages.get(upstream)
        if record is None:
            continue
        if record.params_sha256 != stage_params_sha256(project.profile, upstream):
            warnings.append(
                f"{upstream} ran with different {PROFILE_NAME} values than the "
                f"current ones; its output may be stale for {stage}"
            )
    return warnings


def record_stage(
    project: Project,
    stage: str,
    tool_versions: Mapping[str, str] | None = None,
) -> Project:
    """Record the completion of *stage* in the manifest and persist it.

    Returns:
        A project carrying the updated manifest.
    """
    record = StageRecord(
        done_at=datetime.now(tz=UTC).astimezone(),
        params_sha256=stage_params_sha256(project.profile, stage),
        tool_versions=dict(tool_versions or {}),
    )
    manifest = project.manifest.model_copy(
        update={"stages": {**project.manifest.stages, stage: record}},
        deep=True,
    )
    write_json(project.root / MANIFEST_NAME, manifest)
    return replace(project, manifest=manifest)
