import io
import json
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from scripts import eval_wake, record_positives


def wav_bytes(seconds: float = 0.2, level: int = 0) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        target.writeframes(level.to_bytes(2, "little", signed=True) * round(16_000 * seconds))
    return output.getvalue()


def fake_capture(monkeypatch: pytest.MonkeyPatch, *, interrupt_after: int | None = None) -> list:
    calls = []
    monkeypatch.setattr("builtins.input", lambda _: "")

    def run(command, *, stdout, check):
        assert check
        assert "-y" not in command
        assert command[-1] == "pipe:1"
        calls.append(command)
        if interrupt_after is not None and len(calls) > interrupt_after:
            stdout.write(b"interrupted")
            raise subprocess.CalledProcessError(1, command)
        stdout.write(wav_bytes())

    monkeypatch.setattr(record_positives.subprocess, "run", run)
    return calls


def test_existing_wav_is_never_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_capture(monkeypatch)
    existing = tmp_path / "fr" / "fr_near_01.wav"
    existing.parent.mkdir()
    original = wav_bytes(level=12)
    existing.write_bytes(original)
    with pytest.raises(FileExistsError, match="already contains"):
        record_positives.record_session(tmp_path, 1, 0.2, "default", resume=False)
    assert existing.read_bytes() == original
    assert calls == []


def test_resume_preserves_completed_wavs_and_interrupted_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_capture(monkeypatch, interrupt_after=1)
    with pytest.raises(subprocess.CalledProcessError):
        record_positives.record_session(tmp_path, 1, 0.2, "default", resume=False)
    first = tmp_path / "fr" / "fr_near_01.wav"
    original = first.read_bytes()
    partials = {path: path.read_bytes() for path in tmp_path.glob("*.partial")}
    assert partials
    assert len(list(tmp_path.rglob("*.wav"))) == 1
    calls = fake_capture(monkeypatch)
    record_positives.record_session(tmp_path, 1, 0.2, "default", resume=True)
    assert len(calls) == 3
    assert len(list(tmp_path.rglob("*.wav"))) == 4
    assert first.read_bytes() == original
    assert all(path.read_bytes() == data for path, data in partials.items())
    record_positives.record_session(tmp_path, 1, 0.2, "default", resume=True)
    assert len(calls) == 3


def test_resume_rejects_mismatched_plan_and_incomplete_wav(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_capture(monkeypatch)
    plan = {"version": 1, "count_per_language_distance": 1, "seconds": 0.2}
    (tmp_path / "recording-plan.json").write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="matching"):
        record_positives.record_session(tmp_path, 2, 0.2, "default", resume=True)
    existing = tmp_path / "fr" / "fr_near_01.wav"
    existing.parent.mkdir()
    original = wav_bytes(seconds=0.01)
    existing.write_bytes(original)
    with pytest.raises(ValueError, match="Incomplete"):
        record_positives.record_session(tmp_path, 1, 0.2, "default", resume=True)
    assert existing.read_bytes() == original
    assert calls == []


def test_publication_refuses_a_concurrently_created_wav(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_capture(monkeypatch)
    output = tmp_path / "fr" / "fr_near_01.wav"
    original = wav_bytes(level=30)

    def prompt(_: str) -> str:
        output.parent.mkdir(exist_ok=True)
        output.write_bytes(original)
        return ""

    monkeypatch.setattr("builtins.input", prompt)
    with pytest.raises(FileExistsError):
        record_positives.record_session(tmp_path, 1, 0.2, "default", resume=False)
    assert output.read_bytes() == original
    assert list(tmp_path.glob("*.partial"))


def test_recorded_requires_exact_attestation_and_rejects_copied_synthetic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    synthetic = Path("assets/wake/positives/fr")
    synthetic.mkdir(parents=True)
    (synthetic / "fr_near_01.wav").write_bytes(wav_bytes())
    imported = Path("imports/fr")
    imported.mkdir(parents=True)
    (imported / "fr_near_01.wav").write_bytes(wav_bytes())
    with pytest.raises(ValueError, match="requires --attest-recorded"):
        eval_wake.validate_positive_source(Path("imports"), "recorded", None)
    with pytest.raises(ValueError, match="Known synthetic directory"):
        eval_wake.validate_positive_source(synthetic, "recorded", eval_wake.RECORDED_ATTESTATION)
    with pytest.raises(ValueError, match="matches a known local synthetic"):
        eval_wake.validate_positive_source(
            Path("imports"), "recorded", eval_wake.RECORDED_ATTESTATION
        )
    (imported / "fr_near_01.wav").write_bytes(wav_bytes(level=1))
    provenance = eval_wake.validate_positive_source(
        Path("imports"), "recorded", eval_wake.RECORDED_ATTESTATION
    )
    assert "not independently verified" in provenance
    assert "cannot detect other TTS or identify a speaker" in provenance


def test_cli_rejects_recorded_label_before_loading_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    positives = tmp_path / "imports"
    positives.mkdir()
    for lang in ("fr", "en"):
        (positives / f"{lang}_near_01.wav").write_bytes(wav_bytes())
    monkeypatch.setattr(
        sys, "argv", ["eval_wake", "--positives", str(positives), "--positive-source", "recorded"]
    )
    with pytest.raises(SystemExit) as exc:
        eval_wake.main()
    assert exc.value.code == 2
    assert not Path("docs/wake-eval.md").exists()


def test_import_report_never_claims_verified_speaker_or_complete_corpus(tmp_path: Path) -> None:
    clips = [
        eval_wake.Clip(
            Path(f"{lang}_near_01.wav"), "positive", "user-attested human", lang, 0.2, (0.95,), 100
        )
        for lang in ("fr", "en")
    ]
    report = tmp_path / "report.md"
    eval_wake._write_report(
        report,
        clips,
        [0.9],
        "vosk",
        "recorded",
        f"User attestation: {eval_wake.RECORDED_ATTESTATION}",
    )
    text = report.read_text()
    assert "FR=1, EN=1 user-attested human WAVs" in text
    assert "fr/near=1/20, fr/far=0/20, en/near=1/20, en/far=0/20" in text
    assert "not verified human acceptance" in text
    assert "overwritten or excluded" not in text
    assert "not automatically verified" in text


def test_personal_corpora_excluded_from_docker_and_git() -> None:
    root = Path(__file__).resolve().parents[2]
    assert "assets/wake/" in (root / ".dockerignore").read_text().splitlines()
    assert "assets/wake/recorded/" in (root / ".gitignore").read_text().splitlines()


def test_recording_refuses_synthetic_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    calls = fake_capture(monkeypatch)
    with pytest.raises(ValueError, match="separate"):
        record_positives.record_session(
            Path("assets/wake/positives/new-session"), 1, 0.2, "default", resume=False
        )
    assert calls == []


def test_streaming_wav_header_is_finalized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_capture(monkeypatch)

    def run(command, *, stdout, check):
        data = bytearray(wav_bytes())
        data[4:8] = b"\xff" * 4
        data[40:44] = b"\xff" * 4
        stdout.write(data)

    monkeypatch.setattr(record_positives.subprocess, "run", run)
    record_positives.record_session(tmp_path, 1, 0.2, "default", resume=False)
    for path in tmp_path.rglob("*.wav"):
        with wave.open(str(path), "rb") as source:
            assert source.getnframes() == 3200


def test_cli_rejects_attested_synthetic_copy_before_loading_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    known = Path("assets/wake/positives/fr")
    known.mkdir(parents=True)
    (known / "fr_near_01.wav").write_bytes(wav_bytes())
    imports = Path("imports")
    imports.mkdir()
    for lang in ("fr", "en"):
        (imports / f"{lang}_near_01.wav").write_bytes(wav_bytes())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval_wake",
            "--positives",
            str(imports),
            "--positive-source",
            "recorded",
            "--attest-recorded",
            eval_wake.RECORDED_ATTESTATION,
        ],
    )
    with pytest.raises(SystemExit) as exc:
        eval_wake.main()
    assert exc.value.code == 2
    assert not Path("docs/wake-eval.md").exists()


def test_imported_human_attestation_allows_evaluation_without_identifying_speaker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    positives = Path("imports")
    positives.mkdir()
    for lang in ("fr", "en"):
        (positives / f"{lang}_import.wav").write_bytes(wav_bytes(level=8))
    negatives = Path("negatives")
    negatives.mkdir()
    (negatives / "unknown.wav").write_bytes(wav_bytes())

    class Engine:
        def feed(self, pcm: bytes) -> None:
            return None

        def reset(self) -> None:
            pass

    monkeypatch.setattr(eval_wake, "VoskWakeEngine", lambda **kwargs: Engine())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval_wake",
            "--positives",
            str(positives),
            "--negatives",
            str(negatives),
            "--positive-source",
            "recorded",
            "--attest-recorded",
            eval_wake.RECORDED_ATTESTATION,
        ],
    )
    assert eval_wake.main() == 0
    text = Path("docs/wake-eval.md").read_text()
    assert "FR=1, EN=1 user-attested human WAVs" in text
    assert "not independently verified" in text
    assert "fr/near=0/20" in text
    assert "overwritten or excluded" not in text
    assert "No WAKE_THRESHOLD is selected" in text


def test_resume_requires_plan_and_rejects_unexpected_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_capture(monkeypatch)
    with pytest.raises(ValueError, match="matching"):
        record_positives.record_session(tmp_path, 1, 0.2, "default", resume=True)
    (tmp_path / "recording-plan.json").write_text(
        json.dumps({"version": 1, "count_per_language_distance": 1, "seconds": 0.2})
    )
    extra = tmp_path / "unplanned.wav"
    original = wav_bytes()
    extra.write_bytes(original)
    with pytest.raises(ValueError, match="Unexpected WAVs"):
        record_positives.record_session(tmp_path, 1, 0.2, "default", resume=True)
    assert calls == []
    assert extra.read_bytes() == original
