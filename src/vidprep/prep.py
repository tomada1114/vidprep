"""``prep``: one command from a raw recording to a video ready for YouTube.

Runs the whole pipeline over one file — audio repair, transcription, the
dictionary correction pass, cut detection, the render and the report — and
copies what comes out next to the source material, so the recording and the
thing that gets uploaded sit in the same folder::

    talk01.mp4            the source; read and hashed, never written
    talk01.edited.mp4     silence and filler cut, denoised, -14 LUFS, faded out
    talk01.srt            subtitles on the cut timeline
    talk01.txt            the same transcript as timestamped, paragraphed prose
    talk01.vidprep/       the project: every intermediate JSON, kept for re-runs

The project directory is what makes a second run cheap. A stage whose result is
already there, and whose parameters in ``profile.json`` have not changed since,
is skipped; a stage upstream of one that did run is re-run whether or not its
own parameters moved. So tuning a threshold and running the same command again
re-does exactly what the change reaches, and nothing before it.

That is also how LLM proofreading fits into a pipeline whose CLI is deliberately
AI-free (design.md §7). The first run stops after the dictionary pass and prints
how to run the ``correct-transcript`` skill, which writes ``patch.json`` and
applies it through ``vidprep correct --apply-patch``. Running ``vidprep prep``
again continues from the transcript that was left behind — the pause happens
only on the run that produced the transcript, so it never asks twice. ``--yes``
skips it for an unattended run.

``detect`` approves its own ``silence`` candidates and leaves ``filler`` ones
for a human. This command approves the filler cuts too, because it exists for
something publishable without a review pass — but only while
``filler.enable_weak`` is off. The weak tier ("まあ", "なんか", "こう") is
ordinary Japanese, and the tier a candidate came from is not recorded in
``cuts.json``, so with the weak tier enabled there is no way to approve the
strong ones alone and the whole approval is declined instead. ``--keep-fillers``
turns it off outright, leaving only the silences cut.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from . import audio, correct, detect, render, report, transcribe
from . import project as project_module
from .errors import UsageError
from .models import Cuts

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from .project import Project

#: Suffix of the project directory, placed beside the source material.
PROJECT_SUFFIX = ".vidprep"

#: What is copied out of the project, and the extension it lands under. The
#: video is renamed rather than kept as ``output.mp4`` so that a folder holding
#: several recordings still says which one each render came from. The
#: transcript needs no such renaming — a folder holds one recording's worth of
#: it — so it lands as plainly as the source's own extension is replaced.
EDITED_SUFFIX = ".edited.mp4"
SUBTITLE_SUFFIX = ".srt"
TEXT_SUFFIX = ".txt"

#: The ``reason`` of the candidates this command may approve (design.md §3.4).
FILLER_REASON = "filler"

#: The stages, in pipeline order.
STAGES: tuple[str, ...] = (
    audio.STAGE,
    transcribe.STAGE,
    correct.STAGE,
    detect.STAGE,
    render.STAGE,
    report.STAGE,
)

#: The ``vidprep`` subcommand each stage is equivalent to running (only
#: ``audio_fix`` differs from its CLI name).
_CLI_NAME: Mapping[str, str] = {audio.STAGE: "audio-fix"}

#: How to recognise a finished stage that leaves no record in the manifest.
#: ``report`` is the only one — it writes nothing outside ``report/``, so no
#: stage depends on it and it never earns a record — and without this it would
#: be the one stage that re-ran on every invocation, digest re-encode included.
UNRECORDED_OUTPUT: Mapping[str, Path] = {report.STAGE: Path(report.STATS_NAME)}


class Reported(Protocol):
    """A stage result as this module needs it: something it can report."""

    def lines(self) -> list[str]:
        """The stage's report for a human, one line each."""


@dataclass(frozen=True, slots=True)
class Options:
    """One run of the command, after its arguments have been resolved."""

    video: Path
    project: Path
    pause_for_llm: bool
    verify_asr: bool
    approve_fillers: bool


@dataclass(frozen=True, slots=True)
class Result:
    """What one ``prep`` run did, for both the human lines and ``--json``.

    Every human-readable line is reported through the callback passed to
    :func:`run_prep` as the run progresses — a run takes minutes, so the CLI
    streams it rather than batching it behind the finished result the way a
    single-stage command does. This is what ``--json`` reports afterwards.
    """

    project: Path
    stages_run: tuple[str, ...]
    paused: bool
    delivered: tuple[Path, ...]
    rendered: render.Result | None

    def to_dict(self) -> dict[str, Any]:
        """Render the result as the JSON document ``--json`` prints."""
        return {
            "project": str(self.project),
            "stages_run": list(self.stages_run),
            "paused": self.paused,
            "delivered": [str(path) for path in self.delivered],
            "render": None if self.rendered is None else self.rendered.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class _Reported:
    """A :class:`Reported` for a stage that has no result object of its own."""

    reported: tuple[str, ...]

    def lines(self) -> list[str]:
        """Return the lines this stage reported."""
        return list(self.reported)


def project_for(video: Path) -> Path:
    """Return where the project of *video* lives: ``<video>.vidprep`` beside it."""
    return video.with_name(video.stem + PROJECT_SUFFIX)


def resolve_source(video: Path) -> Path:
    """Return *video* as an absolute path, refusing one that is not a file.

    Raises:
        UsageError: If nothing readable is at that path.
    """
    resolved = video.expanduser().resolve()
    if not resolved.is_file():
        msg = f"source material not found: {resolved}"
        raise UsageError(msg)
    return resolved


def build_options(
    video: Path,
    project: Path | None,
    *,
    yes: bool,
    keep_fillers: bool,
    no_verify_asr: bool,
) -> Options:
    """Resolve one command invocation's arguments into :class:`Options`."""
    resolved = resolve_source(video)
    return Options(
        video=resolved,
        project=(project or project_for(resolved)).expanduser().resolve(),
        pause_for_llm=not yes,
        verify_asr=not no_verify_asr,
        approve_fillers=not keep_fillers,
    )


def _open(project: Path, video: Path, log: Callable[[str], None]) -> None:
    """Create the project for *video* if this is the first run against it."""
    if (project / project_module.MANIFEST_NAME).is_file():
        return
    project_module.init_project(project, video)
    log(f"✔ created {project}")


def _prepare(project: Path) -> Project:
    """Load the project and check it is intact, as every CLI command does."""
    loaded = project_module.load_project(project)
    project_module.verify_source(loaded)
    project_module.validate_artifacts(loaded)
    return loaded


def _needs_run(loaded: Project, stage: str, ran: set[str]) -> bool:
    """Say whether *stage* has work to do, or may be skipped as up to date.

    A stage runs when something it reads was rebuilt in this invocation, when
    it has never run, when the ``profile.json`` sections it is sensitive to
    have changed since it last did — which is what its recorded
    ``params_sha256`` is for — or when an artifact it reads has been rewritten
    since, which is what its recorded ``inputs_sha256`` is for. A stage that
    records nothing is recognised by the file it writes instead
    (:data:`UNRECORDED_OUTPUT`).

    The input check is what makes a correction applied between two runs reach
    the output. ``correct --apply-patch`` rewrites ``transcript.json`` without
    running a pipeline stage, so nothing lands in *ran* and no profile value
    moves; without it the render is skipped as up to date and the subtitles
    keep the old wording, silently and with a zero exit code.
    """
    if any(upstream in ran for upstream in project_module.STAGE_UPSTREAM[stage]):
        return True
    record = loaded.manifest.stages.get(stage)
    if record is None:
        output = UNRECORDED_OUTPUT.get(stage)
        return output is None or not (loaded.root / output).is_file()
    current = project_module.stage_params_sha256(loaded.profile, stage)
    if record.params_sha256 != current:
        return True
    return bool(project_module.stale_inputs(loaded, stage))


def _due(
    project: Path, stage: str, ran: set[str], log: Callable[[str], None]
) -> Project | None:
    """Return the loaded project when *stage* must run, or ``None`` when not."""
    loaded = _prepare(project)
    if not _needs_run(loaded, stage, ran):
        log(f"·  {stage}: up to date")
        return None
    log(f"▶  {stage}")
    return loaded


def _done(
    stage: str, result: Reported, ran: set[str], log: Callable[[str], None]
) -> None:
    """Report what *stage* produced and record that it ran in this invocation."""
    for line in result.lines():
        log(f"   {line}")
    ran.add(stage)


def _dictionary_pass(loaded: Project) -> Reported:
    """Run the misconversion dictionary over the transcript (``vidprep correct``)."""
    path = correct.resolve_dictionary_path(loaded, None)
    dictionary_plan = correct.plan_dictionary(loaded, dictionary_path=path)
    applied = correct.apply(loaded, dictionary_plan)
    return _Reported((f"✔ updated {applied} segments (source={dictionary_plan.tool})",))


def _approve_fillers(project: Path, ran: set[str], log: Callable[[str], None]) -> None:
    """Approve the filler candidates ``detect`` left for a reviewer.

    A candidate somebody already rejected keeps its verdict: the point of the
    approval is to decide the ones nobody has looked at, not to overrule the
    ones somebody has. The rewritten document is validated before it is
    written, so an approval that made two cuts overlap fails here rather than
    inside the render.
    """
    loaded = _prepare(project)
    if loaded.profile.filler.enable_weak:
        log(
            "⚠  filler cuts left as proposed: filler.enable_weak is on and "
            "cuts.json does not record which tier a candidate came from"
        )
        return
    path = project / detect.CUTS_NAME
    if not path.is_file():
        # Only reachable when the file was removed by hand between two runs;
        # `render` says what is missing better than an approval pass can.
        return
    duration = loaded.manifest.source.duration
    document = project_module.load_artifact(path, Cuts, duration)
    approved = [
        cut
        for cut in document.cuts
        if cut.reason == FILLER_REASON and cut.status == "proposed"
    ]
    for cut in approved:
        cut.status = "approved"
    if not approved:
        return
    project_module.write_json(
        path,
        Cuts.model_validate(
            document.model_dump(mode="json"), context={"duration": duration}
        ),
    )
    seconds = sum(cut.end - cut.start for cut in approved)
    log(f"▶  approve: {len(approved)} filler cuts (-{seconds:.1f}s)")
    # cuts.json is what `detect` writes, so everything downstream of `detect`
    # has to be redone for the same reason it would be after a re-detection.
    ran.add(detect.STAGE)


def _deliver(
    project: Path, video: Path, log: Callable[[str], None]
) -> tuple[Path, ...]:
    """Copy the render, its subtitles and its transcript next to the source.

    Returns:
        The files that were written, in the order they were copied.

    Raises:
        UsageError: If the render is not there to copy.
    """
    pairs = (
        (project / render.VIDEO_NAME, video.with_name(video.stem + EDITED_SUFFIX)),
        (
            project / render.SUBTITLES_NAME,
            video.with_name(video.stem + SUBTITLE_SUFFIX),
        ),
        (project / render.TEXT_NAME, video.with_name(video.stem + TEXT_SUFFIX)),
    )
    written = []
    for produced, target in pairs:
        if not produced.is_file():
            msg = (
                f"{produced} was never written; nothing to deliver "
                "— run `vidprep render` first"
            )
            raise UsageError(msg)
        replaced = " (replaced)" if target.exists() else ""
        shutil.copyfile(produced, target)
        log(f"✔ {target}{replaced}")
        written.append(target)
    return tuple(written)


def _llm_instructions(options: Options) -> list[str]:
    """Say how to proofread the transcript, and how to resume afterwards."""
    return [
        "",
        "── the transcript is ready for proofreading ──",
        f"   {options.project / transcribe.TRANSCRIPT_NAME}",
        "",
        "The dictionary pass has run. For the misconversions no dictionary can",
        "decide — a term that is a homophone of an everyday word, a command the",
        "speaker read out loud — run the correct-transcript skill against the",
        "project, which writes patch.json and applies it through the CLI:",
        "",
        f"   cd {options.project}",
        "   claude          then: /correct-transcript",
        "",
        "Then run the same command again to continue from there:",
        "",
        f"   vidprep prep {options.video}",
        "",
        "Or skip the proofreading entirely with --yes.",
    ]


def _transcript_phase(
    options: Options, ran: set[str], log: Callable[[str], None]
) -> bool:
    """Run everything up to and including the dictionary pass.

    Returns:
        Whether the run should stop here so the transcript can be proofread.
        It stops only on the invocation that produced the transcript, so a
        second run continues rather than asking again.
    """
    project = options.project
    if (loaded := _due(project, audio.STAGE, ran, log)) is not None:
        _done(audio.STAGE, audio.run_audio_fix(loaded, with_stats=True), ran, log)
    if (loaded := _due(project, transcribe.STAGE, ran, log)) is not None:
        _done(transcribe.STAGE, transcribe.run_transcribe(loaded), ran, log)
    if (loaded := _due(project, correct.STAGE, ran, log)) is not None:
        _done(correct.STAGE, _dictionary_pass(loaded), ran, log)
    return options.pause_for_llm and transcribe.STAGE in ran


def _video_phase(
    options: Options, ran: set[str], log: Callable[[str], None]
) -> render.Result | None:
    """Detect the cuts, approve the fillers, render and report.

    Returns:
        The render's result, or ``None`` when the existing render was already
        up to date and nothing had to be encoded.
    """
    project = options.project
    if (loaded := _due(project, detect.STAGE, ran, log)) is not None:
        _done(detect.STAGE, detect.run_detect(loaded), ran, log)
    if options.approve_fillers:
        _approve_fillers(project, ran, log)
    rendered: render.Result | None = None
    if (loaded := _due(project, render.STAGE, ran, log)) is not None:
        rendered = render.run_render(loaded, verify_asr=options.verify_asr)
        _done(render.STAGE, rendered, ran, log)
    if (loaded := _due(project, report.STAGE, ran, log)) is not None:
        _done(report.STAGE, report.run_report(loaded), ran, log)
    return rendered


def run_prep(options: Options, log: Callable[[str], None]) -> Result:
    """Run the pipeline over one video and report what happened.

    ``log`` receives one human-readable line at a time as stages complete,
    matching how the CLI streams progress on a run that takes minutes rather
    than batching it behind the finished result the way a single-stage
    command does.
    """
    _open(options.project, options.video, log)
    ran: set[str] = set()
    if _transcript_phase(options, ran, log):
        for line in _llm_instructions(options):
            log(line)
        return Result(options.project, tuple(ran), True, (), None)
    rendered = _video_phase(options, ran, log)
    delivered = _deliver(options.project, options.video, log)
    return Result(options.project, tuple(ran), False, delivered, rendered)


def plan(options: Options) -> dict[str, Any]:
    """Return what :func:`run_prep` would run and write, without doing it.

    The project's manifest and ``profile.json`` are all :func:`_needs_run`
    reads, and both exist before any stage runs, so the stages a real run
    would perform can be worked out without executing any of them. A project
    that has not been created yet has nothing to compare against, so every
    stage up to the pause — or every stage, with ``--yes`` — would run.
    """
    exists = (options.project / project_module.MANIFEST_NAME).is_file()
    if exists:
        loaded = _prepare(options.project)
        would_run, paused = _schedule(loaded, options)
        commands = _commands(would_run)
        writes: list[str] = []
    else:
        paused = options.pause_for_llm
        would_run = list(STAGES[:3]) if paused else list(STAGES)
        commands = [["vidprep", "init"], *_commands(would_run)]
        writes = [str(options.project)]
    if not paused:
        writes += [
            str(options.video.with_name(options.video.stem + EDITED_SUFFIX)),
            str(options.video.with_name(options.video.stem + SUBTITLE_SUFFIX)),
            str(options.video.with_name(options.video.stem + TEXT_SUFFIX)),
        ]
    return {
        "project": str(options.project),
        "stages": would_run,
        "paused": paused,
        "commands": commands,
        "writes": writes,
    }


def _schedule(loaded: Project, options: Options) -> tuple[list[str], bool]:
    """Simulate :func:`_needs_run` over every stage without executing any.

    Returns:
        The stages that would run, in order, and whether the run would pause
        for LLM proofreading before reaching the video-editing stages.
    """
    ran: set[str] = set()
    would_run: list[str] = []
    for stage in (audio.STAGE, transcribe.STAGE, correct.STAGE):
        if _needs_run(loaded, stage, ran):
            would_run.append(stage)
            ran.add(stage)
    paused = options.pause_for_llm and transcribe.STAGE in ran
    if not paused:
        for stage in (detect.STAGE, render.STAGE, report.STAGE):
            if _needs_run(loaded, stage, ran):
                would_run.append(stage)
                ran.add(stage)
    return would_run, paused


def _commands(stages: Sequence[str]) -> list[list[str]]:
    """Render *stages* as the ``vidprep`` invocations they are equivalent to."""
    return [
        ["vidprep", _CLI_NAME.get(stage, stage.replace("_", "-"))] for stage in stages
    ]
