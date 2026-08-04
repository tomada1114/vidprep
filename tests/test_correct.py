"""Tests for the correct stage: dictionary replacement and patch application.

Everything here runs on a hand-made ``transcript.json`` rather than ASR output,
so the stage can be verified before ``transcribe`` exists. The analyser is
faked for the same reason — one class exercises the real SudachiPy reader, the
rest state the readings they rely on so a dictionary update cannot silently
change what a test proves.
"""

from __future__ import annotations

import io
import json
import sys
from typing import TYPE_CHECKING

import pytest

from vidprep import _dictionary, correct
from vidprep import project as project_module
from vidprep._dictionary import AsrDictionary, ReadingToken, normalise_reading
from vidprep.errors import (
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VALIDATION,
    InvariantViolationError,
    PatchInvalidError,
    SchemaInvalidError,
    UsageError,
)
from vidprep.models import Transcript

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

TRANSCRIPT = {
    "version": "1",
    "audio_source": "audio/processed.wav",
    "asr": {"backend": "whisper.cpp", "model": "large-v3-turbo", "vad": "silero"},
    "segments": [
        {"id": "s0001", "start": 0.0, "end": 2.5, "text": "クロードコードで書きました"},
        {"id": "s0002", "start": 2.5, "end": 5.0, "text": "ビッドプレップというツール"},
        {"id": "s0003", "start": 5.0, "end": 7.5, "text": "クロード・コードの話"},
        {"id": "s0004", "start": 7.5, "end": 10.0, "text": "クラウドにデプロイします"},
        {"id": "s0005", "start": 10.0, "end": 12.5, "text": "Claude Code は正しい"},
        {
            "id": "s0006",
            "start": 12.5,
            "end": 15.0,
            "text": "クロードコードとビッドプレップ",
        },
    ],
}

DICTIONARY = AsrDictionary.model_validate(
    {
        "version": "1.0.0",
        "entries": [
            {
                "correct": "Claude Code",
                "misrecognized": ["クロードコード", "クラウドコード"],
                "yomi": "クロードコード",
                "confidence": "always",
            },
            {
                "correct": "Claude",
                "misrecognized": ["クラウド", "クロード"],
                "yomi": "クロード",
                "confidence": "context",
            },
            {
                "correct": "vidprep",
                "misrecognized": ["ビッドプレップ"],
                "yomi": "ビッドプレップ",
                "confidence": "always",
            },
        ],
    }
)

EMPTY_DICTIONARY = AsrDictionary(version="1.0.0")

#: Readings the fake analyser knows. Anything else is read as nothing, which
#: is also how a word SudachiPy cannot read behaves.
READINGS = {
    "クロード": "クロード",
    "コード": "コード",
    "クラウド": "クラウド",
    "ビッドプレップ": "ビッドプレップ",
}


def fake_reader(text: str) -> Sequence[ReadingToken]:
    """Tokenise *text* against :data:`READINGS`, longest known word first."""
    known = sorted(READINGS, key=len, reverse=True)
    tokens = []
    index = 0
    while index < len(text):
        for surface in known:
            if text.startswith(surface, index):
                reading = normalise_reading(READINGS[surface])
                tokens.append(
                    ReadingToken(index, index + len(surface), surface, reading)
                )
                index += len(surface)
                break
        else:
            tokens.append(ReadingToken(index, index + 1, text[index], ""))
            index += 1
    return tokens


def apply_dictionary(text: str, dictionary: AsrDictionary = DICTIONARY) -> str:
    """Return *text* after both dictionary stages, discarding the hit list."""
    return _dictionary.correct_text(text, dictionary, fake_reader)[0]


@pytest.fixture
def transcript_project(project_dir: Path) -> Path:
    """A project holding the hand-made transcript."""
    path = project_dir / "transcript.json"
    path.write_text(json.dumps(TRANSCRIPT, ensure_ascii=False), encoding="utf-8")
    return project_dir


@pytest.fixture
def staged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the CLI use the test dictionary and the fake analyser."""
    monkeypatch.setattr(_dictionary, "load_dictionary", lambda _path=None: DICTIONARY)
    monkeypatch.setattr(_dictionary, "default_reader", lambda: fake_reader)


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Feed the confirmation prompt a canned answer."""

    def _answer(text: str) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(f"{text}\n"))

    return _answer


def read_transcript(directory: Path) -> Transcript:
    """Return the transcript currently stored in *directory*."""
    return Transcript.model_validate_json((directory / "transcript.json").read_bytes())


class TestPackagedDictionary:
    """REQ-001 / REQ-002: the shipped dictionary is valid and carries readings."""

    def test_loads_and_validates(self):
        dictionary = _dictionary.load_dictionary()

        assert dictionary.version
        assert dictionary.entries

    def test_every_entry_has_a_reading(self):
        dictionary = _dictionary.load_dictionary()

        assert all(normalise_reading(entry.yomi) for entry in dictionary.entries)

    def test_every_entry_lists_a_misrecognition(self):
        dictionary = _dictionary.load_dictionary()

        assert all(entry.misrecognized for entry in dictionary.entries)

    def test_unknown_field_is_rejected(self, tmp_path):
        path = tmp_path / "dict.json"
        path.write_text(json.dumps({"version": "1.0.0", "entries": [], "extra": 1}))

        with pytest.raises(SchemaInvalidError, match="extra"):
            _dictionary.load_dictionary(path)

    def test_malformed_json_is_rejected(self, tmp_path):
        path = tmp_path / "dict.json"
        path.write_text("{not json")

        with pytest.raises(SchemaInvalidError, match=r"dict\.json"):
            _dictionary.load_dictionary(path)


class TestDictatedCliTerms:
    """The speaker reads commands out loud, so the ASR writes them as katakana.

    Reproducible misrecognitions belong here; the ones that depend on the
    sentence around them are left to the ``correct-transcript`` skill. Only the
    surface stage is exercised (no reader), because that is the part the
    dictionary decides on its own.
    """

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param(
                "クロード-Cでいきなり最後のセッションに戻れます",
                "claude -cでいきなり最後のセッションに戻れます",
                id="claude-c",
            ),
            pytest.param(
                "先ほどですとリズームを押して",
                "先ほどですとresumeを押して",
                id="resume",
            ),
        ],
    )
    def test_dictated_option_is_restored(self, text, expected):
        corrected, _ = _dictionary.correct_text(text, _dictionary.load_dictionary())

        assert corrected == expected

    def test_context_dependent_misconversion_is_left_to_llm_correction(self):
        # 「半額」 is an everyday word, so 「半角スペース」 cannot be restored
        # without reading the sentence — that is the skill's call, not the
        # dictionary's.
        text = "クロード そして半額スペースに配分し"

        corrected, _ = _dictionary.correct_text(text, _dictionary.load_dictionary())

        assert corrected == text


class TestSurfaceStage:
    """REQ-003: literal misrecognitions are replaced deterministically."""

    def test_replaces_a_known_misrecognition(self):
        assert (
            apply_dictionary("クロードコードで書きました") == "Claude Codeで書きました"
        )

    def test_reports_what_it_replaced(self):
        _, hits = _dictionary.correct_text(
            "ビッドプレップというツール", DICTIONARY, fake_reader
        )

        assert [(hit.stage, hit.matched, hit.correct, hit.applied) for hit in hits] == [
            ("surface", "ビッドプレップ", "vidprep", True)
        ]

    def test_applies_every_entry_that_hits_one_segment(self):
        assert (
            apply_dictionary("クロードコードとビッドプレップ") == "Claude Codeとvidprep"
        )

    def test_longest_match_wins_over_entry_order(self):
        dictionary = AsrDictionary.model_validate(
            {
                "version": "1.0.0",
                "entries": [
                    {
                        "correct": "Claude",
                        "misrecognized": ["クロード"],
                        "yomi": "クロード",
                        "confidence": "always",
                    },
                    {
                        "correct": "Claude Code",
                        "misrecognized": ["クロードコード"],
                        "yomi": "クロードコード",
                        "confidence": "always",
                    },
                ],
            }
        )

        assert apply_dictionary("クロードコードの話", dictionary) == "Claude Codeの話"

    def test_replacement_is_never_rescanned(self):
        """A canonical spelling that another entry misrecognises stays put."""
        dictionary = AsrDictionary.model_validate(
            {
                "version": "1.0.0",
                "entries": [
                    {
                        "correct": "ノート",
                        "misrecognized": ["ノオト"],
                        "yomi": "ノート",
                        "confidence": "always",
                    },
                    {
                        "correct": "note",
                        "misrecognized": ["ノート"],
                        "yomi": "ノート",
                        "confidence": "always",
                    },
                ],
            }
        )

        assert apply_dictionary("ノオトを書く", dictionary) == "ノートを書く"

    def test_an_already_correct_spelling_is_not_reported(self):
        dictionary = AsrDictionary.model_validate(
            {
                "version": "1.0.0",
                "entries": [
                    {
                        "correct": "とまだ",
                        "misrecognized": ["とまだ", "トマダ"],
                        "yomi": "トマダ",
                        "confidence": "always",
                    }
                ],
            }
        )

        text, hits = _dictionary.correct_text("とまだです", dictionary, fake_reader)

        assert (text, hits) == ("とまだです", [])

    def test_an_empty_dictionary_changes_nothing(self):
        text, hits = _dictionary.correct_text("クロードコード", EMPTY_DICTIONARY, None)

        assert (text, hits) == ("クロードコード", [])

    def test_empty_text_changes_nothing(self):
        assert apply_dictionary("") == ""


class TestReadingStage:
    """REQ-004 / REQ-005: unseen spellings are caught by their reading."""

    def test_replaces_a_spelling_only_the_reading_matches(self):
        assert apply_dictionary("クロード・コードの話") == "Claude Codeの話"

    def test_reports_the_stage_that_found_it(self):
        _, hits = _dictionary.correct_text(
            "クロード・コードの話", DICTIONARY, fake_reader
        )

        assert [(hit.stage, hit.matched, hit.applied) for hit in hits] == [
            ("yomi", "クロード・コード", True)
        ]

    def test_a_word_with_no_reading_is_left_to_the_surface_stage(self):
        """The boundary between two known words must be a joiner, not any gap."""
        assert apply_dictionary("クロードXコードの話") == "クロードXコードの話"

    def test_without_an_analyser_only_the_surface_stage_runs(self):
        text, hits = _dictionary.correct_text("クロード・コードの話", DICTIONARY, None)

        assert text == "クロード・コードの話"
        assert [hit.applied for hit in hits] == [False]

    def test_a_reading_longer_than_the_window_is_not_joined(self, monkeypatch):
        monkeypatch.setattr(_dictionary, "MAX_READING_TOKENS", 1)

        assert apply_dictionary("クロード・コードの話") == "クロード・コードの話"


class TestContextEntries:
    """REQ-006: homophones are reported, never replaced (over-replacement = 0)."""

    def test_the_surface_stage_leaves_them_alone(self):
        assert (
            apply_dictionary("クラウドにデプロイします") == "クラウドにデプロイします"
        )

    def test_they_are_still_reported(self):
        _, hits = _dictionary.correct_text(
            "クラウドにデプロイします", DICTIONARY, fake_reader
        )

        assert [(hit.matched, hit.correct, hit.applied) for hit in hits] == [
            ("クラウド", "Claude", False)
        ]

    def test_the_reading_stage_leaves_them_alone(self):
        dictionary = AsrDictionary.model_validate(
            {
                "version": "1.0.0",
                "entries": [
                    {
                        "correct": "Claude",
                        "misrecognized": [],
                        "yomi": "クラウド",
                        "confidence": "context",
                    }
                ],
            }
        )

        assert apply_dictionary("クラウドにデプロイします", dictionary) == (
            "クラウドにデプロイします"
        )


class TestSudachiReader:
    """The real analyser, so the fake one cannot drift away from it."""

    def test_reads_the_packaged_dictionary_terms(self):
        reader = _dictionary.default_reader()
        assert reader is not None, "the dev environment installs sudachidict-core"

        text, hits = _dictionary.correct_text(
            "クロード・コードの話", _dictionary.load_dictionary(), reader
        )

        assert text == "Claude Codeの話"
        assert [hit.stage for hit in hits] == ["yomi"]

    def test_readings_are_normalised_to_katakana(self):
        assert normalise_reading("くろーど・コード") == "クロードコード"
        assert normalise_reading("Claude Code") == ""


class TestDictionaryRun:
    """REQ-007 / REQ-030 / REQ-040 / REQ-041 through the CLI."""

    @pytest.mark.usefixtures("staged")
    def test_updates_only_the_segments_that_changed(self, run_cli, transcript_project):
        result = run_cli("correct", "-p", str(transcript_project))

        assert result.exit_code == EXIT_OK
        assert "updated 4 segments" in result.stdout
        texts = [s.text for s in read_transcript(transcript_project).segments]
        assert texts == [
            "Claude Codeで書きました",
            "vidprepというツール",
            "Claude Codeの話",
            "クラウドにデプロイします",
            "Claude Code は正しい",
            "Claude Codeとvidprep",
        ]

    @pytest.mark.usefixtures("staged")
    def test_records_provenance_and_history(self, run_cli, transcript_project):
        run_cli("correct", "-p", str(transcript_project))

        segment = read_transcript(transcript_project).segments[0]
        assert segment.source == "dict"
        assert [(edit.tool, edit.before) for edit in segment.edits] == [
            ("dict", "クロードコードで書きました")
        ]

    @pytest.mark.usefixtures("staged")
    def test_untouched_segments_keep_their_provenance(
        self, run_cli, transcript_project
    ):
        run_cli("correct", "-p", str(transcript_project))

        segment = read_transcript(transcript_project).segments[3]
        assert (segment.source, segment.edits) == ("asr", [])

    @pytest.mark.usefixtures("staged")
    def test_a_second_run_changes_nothing(self, run_cli, transcript_project):
        run_cli("correct", "-p", str(transcript_project))
        first = (transcript_project / "transcript.json").read_bytes()

        result = run_cli("correct", "-p", str(transcript_project))

        assert "updated 0 segments" in result.stdout
        assert (transcript_project / "transcript.json").read_bytes() == first

    @pytest.mark.usefixtures("staged")
    def test_nothing_but_text_differs(self, run_cli, transcript_project):
        def shape(transcript):
            return [(s.id, s.start, s.end) for s in transcript.segments]

        before = shape(Transcript.model_validate(TRANSCRIPT))

        run_cli("correct", "-p", str(transcript_project))

        assert shape(read_transcript(transcript_project)) == before

    @pytest.mark.usefixtures("staged")
    def test_dry_run_writes_nothing(self, run_cli, transcript_project):
        original = (transcript_project / "transcript.json").read_bytes()

        result = run_cli("correct", "-p", str(transcript_project), "--dry-run")

        assert result.exit_code == EXIT_OK
        assert "4 segments to update" in result.stdout
        assert "dry-run: nothing was written" in result.stdout
        assert (transcript_project / "transcript.json").read_bytes() == original

    @pytest.mark.usefixtures("staged")
    def test_dry_run_lists_what_it_skipped(self, run_cli, transcript_project):
        result = run_cli("correct", "-p", str(transcript_project), "--dry-run")

        assert "s0004: クラウド left alone [skipped: confidence=context]" in (
            result.stdout
        )

    @pytest.mark.usefixtures("staged")
    def test_json_output_is_parsable_on_its_own(self, run_cli, transcript_project):
        result = run_cli("correct", "-p", str(transcript_project), "--json")

        payload = json.loads(result.stdout)
        assert (payload["tool"], payload["changed"], payload["applied"]) == (
            "dict",
            4,
            4,
        )
        assert payload["segments"][0]["id"] == "s0001"
        assert payload["skipped"] == [
            {
                "id": "s0004",
                "stage": "surface",
                "matched": "クラウド",
                "correct": "Claude",
            }
        ]

    def test_an_empty_dictionary_is_not_an_error(
        self, run_cli, transcript_project, monkeypatch
    ):
        monkeypatch.setattr(
            _dictionary, "load_dictionary", lambda _path=None: EMPTY_DICTIONARY
        )

        result = run_cli("correct", "-p", str(transcript_project))

        assert result.exit_code == EXIT_OK
        assert "updated 0 segments" in result.stdout

    def test_a_missing_analyser_only_warns(
        self, run_cli, transcript_project, monkeypatch
    ):
        monkeypatch.setattr(
            _dictionary, "load_dictionary", lambda _path=None: DICTIONARY
        )
        monkeypatch.setattr(_dictionary, "default_reader", lambda: None)

        result = run_cli("correct", "-p", str(transcript_project))

        assert result.exit_code == EXIT_OK
        assert "no usable SudachiDict" in result.stdout
        assert (
            read_transcript(transcript_project).segments[2].text
            == "クロード・コードの話"
        )

    def test_a_project_without_a_transcript_is_a_usage_error(
        self, run_cli, project_dir
    ):
        result = run_cli("correct", "-p", str(project_dir))

        assert result.exit_code == EXIT_USAGE
        assert "run `vidprep transcribe` first" in result.stderr

    @pytest.mark.usefixtures("staged")
    def test_the_stage_is_recorded_in_the_manifest(self, run_cli, transcript_project):
        run_cli("correct", "-p", str(transcript_project))

        loaded = project_module.load_project(transcript_project)
        assert "correct" in loaded.manifest.stages


class TestPatchVerification:
    """REQ-010 / REQ-020 / REQ-021 / REQ-022: reject whole, never in part."""

    @pytest.fixture
    def patch_file(self, tmp_path):
        def _write(payload):
            path = tmp_path / "patch.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return path

        return _write

    def test_an_unknown_id_rejects_the_whole_patch(
        self, run_cli, transcript_project, patch_file
    ):
        original = (transcript_project / "transcript.json").read_bytes()
        path = patch_file(
            {
                "edits": [
                    {"id": "s0001", "text": "OK"},
                    {"id": "s9999", "text": "NG"},
                    {"id": "s0001", "text": "dup"},
                ]
            }
        )

        result = run_cli(
            "correct",
            "-p",
            str(transcript_project),
            "--apply-patch",
            str(path),
            "--yes",
            "--json",
        )

        assert result.exit_code == EXIT_VALIDATION
        assert json.loads(result.stdout) == {
            "error": "patch_invalid",
            "detail": [
                "unknown segment id: s9999",
                "duplicate segment id: s0001",
            ],
            "applied": 0,
        }
        assert (transcript_project / "transcript.json").read_bytes() == original

    def test_a_duplicate_id_is_rejected(self, run_cli, transcript_project, patch_file):
        path = patch_file(
            {"edits": [{"id": "s0002", "text": "a"}, {"id": "s0002", "text": "b"}]}
        )

        result = run_cli(
            "correct",
            "-p",
            str(transcript_project),
            "--apply-patch",
            str(path),
            "--yes",
        )

        assert result.exit_code == EXIT_VALIDATION
        assert "duplicate segment id: s0002" in result.stderr

    @pytest.mark.parametrize(
        ("payload", "complaint"),
        [
            pytest.param({}, "edits", id="missing-edits"),
            pytest.param(
                {"edits": [{"id": "s0001", "text": 1}]}, "text", id="text-not-a-string"
            ),
            pytest.param(
                {"edits": [{"id": "s0001", "text": "a", "start": 0.0}]},
                "start",
                id="timestamp-not-accepted",
            ),
            pytest.param(
                {"edits": [{"id": "nope", "text": "a"}]}, "id", id="malformed-id"
            ),
        ],
    )
    def test_a_schema_violation_is_rejected(
        self, run_cli, transcript_project, patch_file, payload, complaint
    ):
        result = run_cli(
            "correct",
            "-p",
            str(transcript_project),
            "--apply-patch",
            str(patch_file(payload)),
            "--yes",
        )

        assert result.exit_code == EXIT_VALIDATION
        assert complaint in result.stderr

    def test_malformed_json_is_rejected(self, run_cli, transcript_project, tmp_path):
        path = tmp_path / "patch.json"
        path.write_text("{not json")

        result = run_cli(
            "correct",
            "-p",
            str(transcript_project),
            "--apply-patch",
            str(path),
            "--yes",
        )

        assert result.exit_code == EXIT_VALIDATION

    def test_a_missing_patch_file_is_rejected(
        self, run_cli, transcript_project, tmp_path
    ):
        result = run_cli(
            "correct",
            "-p",
            str(transcript_project),
            "--apply-patch",
            str(tmp_path / "absent.json"),
            "--yes",
        )

        assert result.exit_code == EXIT_VALIDATION
        assert "cannot read" in result.stderr

    def test_the_error_lists_every_complaint(self, transcript_project, patch_file):
        loaded = project_module.load_project(transcript_project)
        path = patch_file(
            {"edits": [{"id": "s9998", "text": "a"}, {"id": "s9999", "text": "b"}]}
        )

        with pytest.raises(PatchInvalidError) as raised:
            correct.plan_patch(loaded, path)

        assert raised.value.details == [
            "unknown segment id: s9998",
            "unknown segment id: s9999",
        ]


class TestPatchApplication:
    """REQ-011 / REQ-012 / REQ-013 / REQ-014."""

    @pytest.fixture
    def patch_path(self, tmp_path):
        path = tmp_path / "patch.json"
        path.write_text(
            json.dumps(
                {
                    "edits": [
                        {"id": "s0001", "text": "まるで会話ができる"},
                        {"id": "s0002", "text": ""},
                        {"id": "s0005", "text": "Claude Code は正しい"},
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def test_applies_the_edits_that_change_something(
        self, run_cli, transcript_project, patch_path
    ):
        result = run_cli(
            "correct",
            "-p",
            str(transcript_project),
            "--apply-patch",
            str(patch_path),
            "--yes",
        )

        assert result.exit_code == EXIT_OK
        assert "updated 2 segments (source=llm)" in result.stdout
        segments = read_transcript(transcript_project).segments
        assert segments[0].text == "まるで会話ができる"
        assert segments[1].text == ""
        assert (segments[4].source, segments[4].edits) == ("asr", [])

    def test_records_provenance_and_history(
        self, run_cli, transcript_project, patch_path
    ):
        run_cli(
            "correct",
            "-p",
            str(transcript_project),
            "--apply-patch",
            str(patch_path),
            "--yes",
        )

        segment = read_transcript(transcript_project).segments[0]
        assert segment.source == "llm"
        assert [(edit.tool, edit.before) for edit in segment.edits] == [
            ("llm", "クロードコードで書きました")
        ]

    def test_shows_what_it_verified_and_what_would_change(
        self, run_cli, transcript_project, patch_path
    ):
        result = run_cli(
            "correct",
            "-p",
            str(transcript_project),
            "--apply-patch",
            str(patch_path),
            "--yes",
        )

        assert "verified: 3 known ids, no duplicates" in result.stdout
        assert "s0001: クロードコードで書きました" in result.stdout
        assert "2 segments to update" in result.stdout

    def test_an_emptied_segment_is_highlighted(
        self, run_cli, transcript_project, patch_path
    ):
        result = run_cli(
            "correct",
            "-p",
            str(transcript_project),
            "--apply-patch",
            str(patch_path),
            "--yes",
        )

        assert "-> <empty>" in result.stdout

    def test_without_yes_it_asks_first(
        self, run_cli, transcript_project, patch_path, answers
    ):
        answers("y")

        result = run_cli(
            "correct", "-p", str(transcript_project), "--apply-patch", str(patch_path)
        )

        assert result.exit_code == EXIT_OK
        assert "Apply 2 changes?" in result.stdout
        assert (
            read_transcript(transcript_project).segments[0].text == "まるで会話ができる"
        )

    def test_declining_writes_nothing(
        self, run_cli, transcript_project, patch_path, answers
    ):
        original = (transcript_project / "transcript.json").read_bytes()
        answers("n")

        result = run_cli(
            "correct", "-p", str(transcript_project), "--apply-patch", str(patch_path)
        )

        assert result.exit_code == EXIT_USAGE
        assert (transcript_project / "transcript.json").read_bytes() == original

    def test_dry_run_writes_nothing(self, run_cli, transcript_project, patch_path):
        original = (transcript_project / "transcript.json").read_bytes()

        result = run_cli(
            "correct",
            "-p",
            str(transcript_project),
            "--apply-patch",
            str(patch_path),
            "--dry-run",
        )

        assert result.exit_code == EXIT_OK
        assert (transcript_project / "transcript.json").read_bytes() == original

    def test_an_empty_patch_changes_nothing(
        self, run_cli, transcript_project, tmp_path
    ):
        path = tmp_path / "patch.json"
        path.write_text(json.dumps({"edits": []}))

        result = run_cli(
            "correct",
            "-p",
            str(transcript_project),
            "--apply-patch",
            str(path),
            "--yes",
        )

        assert result.exit_code == EXIT_OK
        assert "updated 0 segments" in result.stdout


class TestInvariantGuard:
    """REQ-011 / REQ-041: a correction that moved a timestamp is thrown away."""

    def test_a_moved_timestamp_stops_the_write(self, transcript_project):
        loaded = project_module.load_project(transcript_project)
        original = (transcript_project / "transcript.json").read_bytes()
        plan = correct.plan_dictionary(loaded, EMPTY_DICTIONARY, fake_reader)
        moved = plan.corrected.model_copy(deep=True)
        moved.segments[0].end = 99.0
        broken = correct.Plan(
            tool="dict",
            original=plan.original,
            corrected=moved,
            changes=(correct.SegmentChange("s0001", "before", "after"),),
        )

        with pytest.raises(InvariantViolationError, match="more than text"):
            correct.apply(loaded, broken)

        assert (transcript_project / "transcript.json").read_bytes() == original

    def test_a_dropped_segment_stops_the_write(self, transcript_project):
        loaded = project_module.load_project(transcript_project)
        plan = correct.plan_dictionary(loaded, EMPTY_DICTIONARY, fake_reader)
        shortened = plan.corrected.model_copy(
            update={"segments": plan.corrected.segments[:-1]}
        )
        broken = correct.Plan(
            tool="dict",
            original=plan.original,
            corrected=shortened,
            changes=(correct.SegmentChange("s0001", "before", "after"),),
        )

        with pytest.raises(InvariantViolationError, match="segment count"):
            correct.apply(loaded, broken)


class TestTranscriptLoading:
    def test_a_missing_transcript_is_a_usage_error(self, project_dir):
        loaded = project_module.load_project(project_dir)

        with pytest.raises(UsageError, match="transcribe"):
            correct.load_transcript(loaded)

    def test_an_invalid_transcript_is_a_schema_error(self, transcript_project):
        (transcript_project / "transcript.json").write_text('{"version": "2"}')
        loaded = project_module.load_project(transcript_project)

        with pytest.raises(SchemaInvalidError, match=r"transcript\.json"):
            correct.load_transcript(loaded)
