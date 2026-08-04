"""The ``audio-fix`` stage: denoise, high-pass, loudness normalisation.

The chain is ``denoise -> highpass 80Hz -> loudnorm`` in two passes, run in
linear mode so the gain applied is constant and the recording does not pump
(design.md §5.1). Everything downstream — ASR and render alike — reads the
``audio/processed.wav`` this produces, so the stage guarantees two things: the
source material is only ever read, and the length does not change.

Length invariance needs help from the chain rather than luck, because two of
its steps quietly move the end of the recording: DeepFilterNet compensates its
own STFT and lookahead delay by trimming that much off the tail, and loudnorm
labels its output with timestamps offset by its lookahead. Rebuilding the
timestamps and padding back to the source length undoes both without shifting
the timeline the transcript will be built on; the result is then re-probed, so
the guarantee is verified rather than assumed.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import _ffmpeg, doctor
from . import project as project_module
from .errors import ExecutionFailedError, InvariantViolationError, UsageError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .models import Loudnorm
    from .project import Project

STAGE = "audio_fix"
OUTPUT_NAME = Path("audio") / "processed.wav"

DEEPFILTERNET = "deepfilternet"
AFFTDN = "afftdn"
DENOISERS = (DEEPFILTERNET, AFFTDN)

#: PCM 16 bit, at the source's own sample rate and channel count (design.md §5.1).
SAMPLE_CODEC = "pcm_s16le"

#: The noise floor is read from the same "silence" the detection stage uses,
#: so the two stages never disagree about which parts of the take are quiet.
SILENCE_NOISE = "-40dB"
SILENCE_MIN_SECONDS = 0.5

#: Appended to the pass-2 chain so ``-t`` can cut at the source length: fresh
#: timestamps from the sample count, then silence to pad against.
LENGTH_TAIL = "asetpts=N/SR/TB,apad"

MAX_DELTA_MS = 1.0
MS_PER_SECOND = 1000.0
DELTA_DECIMALS = 3
WORKSPACE_PREFIX = ".audio-fix-"
EXTRACTED_NAME = "source.wav"
RENDERED_NAME = "processed.wav"
DENOISED_DIR = "denoised"

#: Pass-2 loudnorm option -> the pass-1 report field that supplies its value.
MEASURED_KEYS = {
    "measured_I": "input_i",
    "measured_TP": "input_tp",
    "measured_LRA": "input_lra",
    "measured_thresh": "input_thresh",
    "offset": "target_offset",
}

#: What ``--dry-run`` prints where only a real run can supply a number.
PLACEHOLDERS = {option: f"<{option}>" for option in MEASURED_KEYS}
INTERVALS_PLACEHOLDER = "<silence intervals>"

_LOUDNORM_REPORT = re.compile(r'\{[^{}]*"input_i"[^{}]*\}')
_SILENCE_EVENT = re.compile(r"silence_(start|end):\s*(-?\d+(?:\.\d+)?)")
_RMS_LEVEL = re.compile(r"Overall.*?RMS level dB:\s*(\S+)", re.DOTALL)


@dataclass(frozen=True, slots=True)
class Measurement:
    """The four numbers ``--stats`` compares before and after the chain."""

    integrated_lufs: float
    true_peak_dbtp: float
    lra: float
    noise_floor_rms_db: float | None

    def to_dict(self) -> dict[str, float | None]:
        """Render the measurement as the JSON object ``--stats`` prints."""
        floor = self.noise_floor_rms_db
        return {
            "integrated_lufs": round(self.integrated_lufs, 2),
            "true_peak_dbtp": round(self.true_peak_dbtp, 2),
            "lra": round(self.lra, 2),
            "noise_floor_rms_db": None if floor is None else round(floor, 2),
        }


@dataclass(frozen=True, slots=True)
class Chain:
    """The filter chain to apply, resolved against the installed tools.

    Attributes:
        denoise: The denoiser that will actually run, after any fallback.
        denoiser_path: Absolute path of the DeepFilterNet binary, when used.
        denoiser_version: Version of that binary, recorded as provenance.
        highpass_hz: Corner frequency of the high-pass that follows denoising.
        loudnorm: EBU R128 targets taken from ``profile.json``.
        sample_rate: Sample rate to preserve, from the source material.
        channels: Channel count to preserve, from the source material.
    """

    denoise: str
    denoiser_path: str | None
    denoiser_version: str | None
    highpass_hz: int
    loudnorm: Loudnorm
    sample_rate: int
    channels: int

    @property
    def uses_deepfilternet(self) -> bool:
        """Whether denoising runs as a separate process before ffmpeg."""
        return self.denoise == DEEPFILTERNET

    def filters(self, measured: Mapping[str, str] | None) -> str:
        """Return the whole chain: denoise (when in-band), high-pass, loudnorm."""
        stages = [] if self.uses_deepfilternet else [AFFTDN]
        stages.append(f"highpass=f={self.highpass_hz}")
        stages.append(_loudnorm_filter(self.loudnorm, measured))
        return ",".join(stages)

    def extract_command(self, source: Path, target: Path) -> list[str]:
        """Return the command that decodes the source audio into a wav."""
        return [
            _ffmpeg.FFMPEG,
            *_ffmpeg.WRITING,
            "-i",
            str(source),
            "-map",
            "0:a:0",
            *self._output_format(),
            str(target),
        ]

    def denoise_command(self, source: Path, out_dir: Path) -> list[str]:
        """Return the DeepFilterNet command line for *source*."""
        return [
            self.denoiser_path or doctor.DEEPFILTERNET_BINARIES[0],
            "--compensate-delay",
            "--output-dir",
            str(out_dir),
            str(source),
        ]

    def analysis_command(self, source: Path) -> list[str]:
        """Return the loudnorm pass-1 command: the full chain, measuring only."""
        return _analysis_command(source, self.filters(None))

    def measure_command(self, source: Path) -> list[str]:
        """Return the command that measures *source* as it is, chain excluded."""
        return measurement_command(source, self.loudnorm)

    def render_command(
        self,
        source: Path,
        target: Path,
        measured: Mapping[str, str],
        seconds: float | str,
    ) -> list[str]:
        """Return the loudnorm pass-2 command, capped at *seconds*.

        The tail is what makes the length invariant hold. ``loudnorm`` labels
        its output with timestamps offset by its own lookahead — the samples
        are all there, but ``-t`` would cut that much short — so the
        timestamps are rebuilt from the sample count before the stream is
        padded with silence and cut at the length the source audio had.
        """
        length = seconds if isinstance(seconds, str) else f"{seconds:.6f}"
        return [
            _ffmpeg.FFMPEG,
            *_ffmpeg.WRITING,
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-af",
            f"{self.filters(measured)},{LENGTH_TAIL}",
            "-t",
            length,
            *self._output_format(),
            str(target),
        ]

    def _output_format(self) -> list[str]:
        """Return the output options that pin the format of every wav written."""
        return [
            "-c:a",
            SAMPLE_CODEC,
            "-ar",
            str(self.sample_rate),
            "-ac",
            str(self.channels),
        ]

    def tool_versions(self) -> dict[str, str]:
        """Return the versions of the tools that produced this stage's output."""
        versions = {"ffmpeg": doctor.check_ffmpeg().get("version") or "unknown"}
        if self.uses_deepfilternet:
            versions[DEEPFILTERNET] = self.denoiser_version or "unknown"
        return versions


@dataclass(frozen=True, slots=True)
class Result:
    """What one ``audio-fix`` run produced and measured."""

    denoise_used: str
    source_duration: float
    output_duration: float
    before: Measurement | None = None
    after: Measurement | None = None
    warnings: tuple[str, ...] = ()

    @property
    def delta_ms(self) -> float:
        """How much the chain changed the length, in milliseconds."""
        return abs(self.output_duration - self.source_duration) * MS_PER_SECOND

    def to_dict(self) -> dict[str, Any]:
        """Render the result as the JSON document ``--json`` prints."""
        payload: dict[str, Any] = {"denoise_used": self.denoise_used}
        if self.before is not None and self.after is not None:
            payload["before"] = self.before.to_dict()
            payload["after"] = self.after.to_dict()
        payload["duration"] = {
            "source": round(self.source_duration, DELTA_DECIMALS),
            "output": round(self.output_duration, DELTA_DECIMALS),
            "delta_ms": round(self.delta_ms, DELTA_DECIMALS),
        }
        payload["output"] = str(OUTPUT_NAME)
        if self.warnings:
            payload["warnings"] = list(self.warnings)
        return payload

    def lines(self) -> list[str]:
        """Render the result for a human, warnings first."""
        detail = []
        if self.before is not None and self.after is not None:
            detail += [
                f"{self.before.integrated_lufs:.2f} → "
                f"{self.after.integrated_lufs:.2f} LUFS",
                f"TP {self.after.true_peak_dbtp:.2f} dBTP",
            ]
        detail += [f"denoise: {self.denoise_used}", f"delta {self.delta_ms:.1f}ms"]
        reported = [f"⚠ {warning}" for warning in self.warnings]
        reported.append(f"✔ {OUTPUT_NAME} ({', '.join(detail)})")
        return reported


def _analysis_command(source: Path, filters: str) -> list[str]:
    """Return an ffmpeg command that decodes *source* only to measure it."""
    return [
        _ffmpeg.FFMPEG,
        *_ffmpeg.QUIET,
        "-v",
        "info",
        "-i",
        str(source),
        "-af",
        filters,
        "-f",
        "null",
        "-",
    ]


def _loudnorm_filter(targets: Loudnorm, measured: Mapping[str, str] | None) -> str:
    """Return the ``loudnorm`` filter for an analysis (``None``) or pass 2."""
    options = [
        f"I={targets.i:g}",
        f"TP={targets.tp:g}",
        f"LRA={targets.lra:g}",
    ]
    if measured is not None:
        options += [f"{option}={measured[option]}" for option in MEASURED_KEYS]
        options.append("linear=true")
    options.append("print_format=json")
    return "loudnorm=" + ":".join(options)


def measurement_command(source: Path, targets: Loudnorm) -> list[str]:
    """Return the command that measures *source* against *targets*, unchanged.

    The filter only reports: ``report`` uses it to state what the material, the
    processed audio and the rendered output each measure, without applying the
    chain to any of them.
    """
    return _analysis_command(source, _loudnorm_filter(targets, None))


def silence_command(source: Path) -> list[str]:
    """Return the command that lists the silent stretches of *source*."""
    filters = f"silencedetect=noise={SILENCE_NOISE}:d={SILENCE_MIN_SECONDS:g}"
    return _analysis_command(source, filters)


def noise_floor_command(source: Path, expression: str) -> list[str]:
    """Return the command that measures the RMS level of *expression* only."""
    filters = (
        f"aselect='{expression}',asetpts=N/SR/TB,"
        "astats=measure_perchannel=none:measure_overall=RMS_level"
    )
    return _analysis_command(source, filters)


def interval_expression(intervals: Sequence[tuple[float, float]]) -> str:
    """Return the ``aselect`` expression that keeps only *intervals*."""
    return "+".join(f"between(t,{start:.3f},{end:.3f})" for start, end in intervals)


def _loudnorm_report(text: str) -> dict[str, str]:
    """Parse the JSON block loudnorm printed into *text*.

    Raises:
        ExecutionFailedError: If loudnorm printed nothing vidprep can read; the
            run stops here rather than writing output from guessed numbers.
    """
    match = _LOUDNORM_REPORT.search(text)
    if match is None:
        msg = "loudnorm printed no JSON report to read the measured values from"
        raise ExecutionFailedError(msg)
    try:
        report = json.loads(match.group(0))
        return {str(key): str(value) for key, value in report.items()}
    except (json.JSONDecodeError, AttributeError) as exc:
        msg = f"could not read the loudnorm analysis: {exc}"
        raise ExecutionFailedError(msg) from exc


def _measured_options(report: Mapping[str, str]) -> dict[str, str]:
    """Return the pass-2 options carrying the pass-1 measurements.

    Raises:
        ExecutionFailedError: If the report lacks a value pass 2 needs.
    """
    missing = [key for key in MEASURED_KEYS.values() if key not in report]
    if missing:
        msg = f"the loudnorm analysis is missing {', '.join(missing)}"
        raise ExecutionFailedError(msg)
    return {option: report[key] for option, key in MEASURED_KEYS.items()}


def _loudness(report: Mapping[str, str]) -> tuple[float, float, float]:
    """Return integrated loudness, true peak and LRA read from *report*.

    Raises:
        ExecutionFailedError: If a field is absent or not a number.
    """
    try:
        return (
            float(report["input_i"]),
            float(report["input_tp"]),
            float(report["input_lra"]),
        )
    except (KeyError, ValueError) as exc:
        msg = f"could not read the loudness measurement: {exc}"
        raise ExecutionFailedError(msg) from exc


def _silence_intervals(text: str, seconds: float) -> list[tuple[float, float]]:
    """Return the silent stretches silencedetect reported in *text*.

    A stretch that runs to the end of the recording has no ``silence_end``
    event in some ffmpeg builds, so an unclosed one is closed at *seconds*.
    """
    intervals: list[tuple[float, float]] = []
    start: float | None = None
    for kind, value in _SILENCE_EVENT.findall(text):
        if kind == "start":
            start = float(value)
        elif start is not None:
            intervals.append((start, float(value)))
            start = None
    if start is not None:
        intervals.append((start, seconds))
    return [pair for pair in intervals if pair[1] - pair[0] >= SILENCE_MIN_SECONDS]


def _rms_level(text: str) -> float | None:
    """Return the overall RMS level astats reported, or ``None`` if it is silent.

    Raises:
        ExecutionFailedError: If astats printed no overall RMS level at all.
    """
    match = _RMS_LEVEL.search(text)
    if match is None:
        msg = "astats printed no overall RMS level"
        raise ExecutionFailedError(msg)
    try:
        level = float(match.group(1))
    except ValueError:
        return None  # a level astats could not compute is reported as unknown
    # Digital silence reads as -inf, which is a true answer but not a number.
    return level if math.isfinite(level) else None


def detect_silence(path: Path, seconds: float) -> list[tuple[float, float]]:
    """Return the silent stretches of *path*, in seconds.

    Args:
        path: The audio or video file to look at.
        seconds: Its duration, used to close a stretch that runs to the end.

    Returns:
        The intervals quieter than :data:`SILENCE_NOISE` for at least
        :data:`SILENCE_MIN_SECONDS`, in timeline order.
    """
    return _silence_intervals(_ffmpeg.run_analysis(silence_command(path)), seconds)


def measure(
    path: Path, targets: Loudnorm, intervals: Sequence[tuple[float, float]] = ()
) -> Measurement:
    """Measure loudness and, when there is silence to look at, the noise floor.

    Args:
        path: The audio or video file to measure.
        targets: The EBU R128 targets the loudnorm analysis is run against;
            they do not change what is measured, only what it is compared with.
        intervals: The silence the noise floor is read over. Without it the
            floor is reported as unknown rather than guessed at.

    Returns:
        The four numbers this stage and ``report`` both quote.
    """
    integrated, true_peak, lra = _loudness(
        _loudnorm_report(_ffmpeg.run_analysis(measurement_command(path, targets)))
    )
    floor = None
    if intervals:
        command = noise_floor_command(path, interval_expression(intervals))
        floor = _rms_level(_ffmpeg.run_analysis(command))
    return Measurement(integrated, true_peak, lra, floor)


def _compare(
    chain: Chain, sources: tuple[Path, Path], seconds: float
) -> tuple[Measurement, Measurement, list[str]]:
    """Measure the audio before and after the chain over the same silence.

    Returns:
        The two measurements and any warning about what could not be measured.
    """
    original, produced = sources
    intervals = detect_silence(original, seconds)
    warnings = []
    if not intervals:
        warnings.append(
            f"no silence of {SILENCE_MIN_SECONDS:g}s at {SILENCE_NOISE} was found; "
            "the noise floor could not be measured"
        )
    return (
        measure(original, chain.loudnorm, intervals),
        measure(produced, chain.loudnorm, intervals),
        warnings,
    )


def resolve_chain(loaded: Project) -> tuple[Chain, list[str]]:
    """Decide which chain this machine can run for *loaded*.

    Returns:
        The chain and the warnings the user should see, which is how the
        DeepFilterNet fallback announces itself.

    Raises:
        UsageError: If ``profile.json`` names a denoiser vidprep does not have.
    """
    settings = loaded.profile.audio
    if settings.denoise not in DENOISERS:
        msg = (
            f"profile.json: audio.denoise must be one of {', '.join(DENOISERS)} "
            f"(found {settings.denoise!r})"
        )
        raise UsageError(msg)

    warnings = []
    denoise = settings.denoise
    path = version = None
    if denoise == DEEPFILTERNET:
        check = doctor.check_deepfilternet()
        if check["ok"]:
            path = str(check["path"])
            version = check["version"]
        else:
            warnings.append(f"{check['error']}; falling back to {AFFTDN}")
            denoise = AFFTDN
    stream = loaded.manifest.source.audio
    chain = Chain(
        denoise=denoise,
        denoiser_path=path,
        denoiser_version=version,
        highpass_hz=settings.highpass_hz,
        loudnorm=settings.loudnorm,
        sample_rate=stream.sample_rate,
        channels=stream.channels,
    )
    return chain, warnings


def plan(loaded: Project, *, with_stats: bool = False) -> dict[str, Any]:
    """Return what :func:`run_audio_fix` would run and write, without doing it."""
    chain, warnings = resolve_chain(loaded)
    workspace = loaded.root / f"{WORKSPACE_PREFIX}XXXXXX"
    extracted = workspace / EXTRACTED_NAME
    rendered = workspace / RENDERED_NAME
    denoised = extracted
    commands = [chain.extract_command(loaded.source_path, extracted)]
    if chain.uses_deepfilternet:
        denoised = workspace / DENOISED_DIR / EXTRACTED_NAME
        commands.append(chain.denoise_command(extracted, workspace / DENOISED_DIR))
    commands += [
        chain.analysis_command(denoised),
        chain.render_command(denoised, rendered, PLACEHOLDERS, "<source duration>"),
        _ffmpeg.duration_command(rendered),
    ]
    if with_stats:
        commands.append(silence_command(extracted))
        for path in (extracted, rendered):
            commands.append(chain.measure_command(path))
            commands.append(noise_floor_command(path, INTERVALS_PLACEHOLDER))
    return {
        "action": "audio-fix",
        "project": str(loaded.root),
        "denoise_used": chain.denoise,
        "commands": commands,
        "writes": [
            str(loaded.root / OUTPUT_NAME),
            str(loaded.root / project_module.MANIFEST_NAME),
        ],
        "warnings": warnings,
    }


def _denoise(chain: Chain, source: Path, out_dir: Path) -> Path:
    """Run DeepFilterNet over *source* and return the file it wrote.

    Raises:
        ExecutionFailedError: If the denoiser exited cleanly but wrote nothing.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    _ffmpeg.run(chain.denoise_command(source, out_dir))
    produced = out_dir / source.name
    if not produced.is_file():
        msg = f"{DEEPFILTERNET} wrote no output for {source.name}"
        raise ExecutionFailedError(msg)
    return produced


def _check_duration(source_seconds: float, output_seconds: float) -> None:
    """Enforce the length invariant of design.md §5.1.

    Raises:
        InvariantViolationError: If the length moved by more than 1ms, which
            makes every timestamp the later stages compute untrustworthy.
    """
    delta = abs(output_seconds - source_seconds) * MS_PER_SECOND
    if round(delta, DELTA_DECIMALS) > MAX_DELTA_MS:
        msg = (
            f"audio-fix changed the length: source {source_seconds:.3f}s, output "
            f"{output_seconds:.3f}s (delta {delta:.1f}ms > {MAX_DELTA_MS:g}ms); "
            f"{OUTPUT_NAME} was left untouched"
        )
        raise InvariantViolationError(msg)


def _produce(
    loaded: Project, chain: Chain, workspace: Path, *, with_stats: bool
) -> Result:
    """Run the chain inside *workspace* and publish the result on success."""
    extracted = workspace / EXTRACTED_NAME
    _ffmpeg.run(chain.extract_command(loaded.source_path, extracted))
    seconds = _ffmpeg.duration(extracted)

    denoised = extracted
    if chain.uses_deepfilternet:
        denoised = _denoise(chain, extracted, workspace / DENOISED_DIR)

    analysis = _ffmpeg.run_analysis(chain.analysis_command(denoised))
    measured = _measured_options(_loudnorm_report(analysis))
    rendered = workspace / RENDERED_NAME
    _ffmpeg.run(chain.render_command(denoised, rendered, measured, seconds))

    produced = _ffmpeg.duration(rendered)
    _check_duration(seconds, produced)

    before = after = None
    warnings: list[str] = []
    if with_stats:
        before, after, warnings = _compare(chain, (extracted, rendered), seconds)
    project_module.atomic_replace(rendered, loaded.root / OUTPUT_NAME)
    return Result(chain.denoise, seconds, produced, before, after, tuple(warnings))


def run_audio_fix(loaded: Project, *, with_stats: bool = False) -> Result:
    """Run the audio-fix chain for *loaded* and record it in the manifest.

    Args:
        loaded: The project to process; its source material is only read.
        with_stats: Also measure loudness and noise floor before and after.

    Returns:
        What was produced, including the before/after statistics when asked.

    Raises:
        InvariantViolationError: If the output length differs by more than 1ms.
    """
    chain, warnings = resolve_chain(loaded)
    workspace = Path(tempfile.mkdtemp(dir=loaded.root, prefix=WORKSPACE_PREFIX))
    try:
        result = _produce(loaded, chain, workspace, with_stats=with_stats)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    project_module.record_stage(loaded, STAGE, chain.tool_versions())
    return replace(result, warnings=(*warnings, *result.warnings))
