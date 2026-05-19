"""End-to-end refiner test using a fake embedder.

The real embedder requires SpeechBrain + torch + a ~22 MB model
download, which we skip here. Instead we inject a deterministic
embedder that returns a fixed vector per turn based on a "voice id"
derived from the synthetic audio's dominant frequency. That's enough
to exercise: segmenter -> embedder -> clusterer -> store-match ->
per-segment label assignment.
"""
from __future__ import annotations

import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from meeting_notetaker.diarization import segmenter as _segmenter
from meeting_notetaker.diarization.refiner import (
    SegmentLabel,
    apply_labels_to_segments,
    refine_loopback,
)
from meeting_notetaker.diarization.store import SpeakerStore
from meeting_notetaker.models.transcript import TranscriptSegment


@pytest.fixture(autouse=True)
def _disable_silero_for_refiner_tests(monkeypatch):
    """Force find_turns through webrtcvad. These tests feed synthetic
    sine waves; silero (correctly) classifies them as non-speech.
    See the matching fixture in test_diarization_segmenter.py.
    """
    monkeypatch.setattr(
        _segmenter,
        "_voiced_mask_silero",
        lambda pcm_16k, **_kwargs: np.zeros(0, dtype=bool),
    )
    monkeypatch.setattr(_segmenter, "_silero_load_attempted", False)
    monkeypatch.setattr(_segmenter, "_silero_model", None)


def _sine(freq_hz: float, duration_sec: float, sample_rate: int, amplitude: float = 0.5) -> np.ndarray:
    t = np.linspace(0, duration_sec, int(duration_sec * sample_rate), endpoint=False)
    return (np.sin(2 * np.pi * freq_hz * t) * amplitude * 32767).astype(np.int16)


def _silence(duration_sec: float, sample_rate: int) -> np.ndarray:
    return np.zeros(int(duration_sec * sample_rate), dtype=np.int16)


def _unit_vec(*components: float) -> np.ndarray:
    arr = np.asarray(components, dtype=np.float32)
    return arr / np.linalg.norm(arr)


class FrequencyEmbedder:
    """Maps each Turn to a fixed 3-dim vector based on its dominant tone.

    Uses an FFT peak as the discriminator. Same frequency -> same
    embedding (within noise) -> same cluster. Lets us simulate the
    real ECAPA-TDNN encoder behavior without the heavy dep.
    """

    def embed_turn(self, turn) -> np.ndarray:
        pcm = turn.pcm.astype(np.float32)
        if pcm.size == 0:
            return np.zeros(3, dtype=np.float32)
        spectrum = np.abs(np.fft.rfft(pcm))
        peak_idx = int(np.argmax(spectrum))
        freqs = np.fft.rfftfreq(pcm.size, d=1.0 / turn.sample_rate)
        peak_freq = float(freqs[peak_idx]) if peak_idx < freqs.size else 0.0
        # Bucket-discrete output so two turns at "the same voice" land in
        # the same place even with slight numerical drift.
        if peak_freq < 150:
            return _unit_vec(1.0, 0.0, 0.0)
        if peak_freq < 350:
            return _unit_vec(0.0, 1.0, 0.0)
        return _unit_vec(0.0, 0.0, 1.0)


def _write_wav(path: Path, pcm: np.ndarray, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())


@pytest.fixture
def two_voice_wav(tmp_path):
    """A loopback wav with two distinct voices interleaved with silence."""
    sr = 16000
    pcm = np.concatenate([
        _silence(0.6, sr),
        _sine(200, 2.0, sr),       # voice A, 0.6-2.6s
        _silence(0.8, sr),
        _sine(500, 2.0, sr),       # voice B, 3.4-5.4s
        _silence(0.8, sr),
        _sine(200, 2.0, sr),       # voice A again, 6.2-8.2s
    ])
    wav_path = tmp_path / "sys.wav"
    _write_wav(wav_path, pcm, sr)
    return wav_path


@pytest.fixture
def two_voice_segments():
    """Transcript segments matching the synthetic timeline above."""
    return [
        TranscriptSegment(source="sys", text="Hello from voice A",  t_start=0.6, t_end=2.6),
        TranscriptSegment(source="mic", text="My mic line",          t_start=3.0, t_end=3.3),
        TranscriptSegment(source="sys", text="Hello from voice B",   t_start=3.4, t_end=5.4),
        TranscriptSegment(source="sys", text="Voice A again",        t_start=6.2, t_end=8.2),
    ]


def test_refiner_groups_recurring_voice_into_one_cluster(two_voice_wav, two_voice_segments, tmp_path):
    store = SpeakerStore(tmp_path / "speakers.db")
    result = refine_loopback(
        two_voice_wav,
        two_voice_segments,
        speaker_store=store,
        encoder=FrequencyEmbedder(),
    )
    # Two distinct voices in the loopback -> two clusters.
    assert len(result.clusters) == 2
    # Both occurrences of voice A end up in the same cluster.
    label_a_first = next(
        lbl for lbl in result.segment_labels if lbl.segment_index == 0
    )
    label_a_second = next(
        lbl for lbl in result.segment_labels if lbl.segment_index == 3
    )
    assert label_a_first.cluster_id == label_a_second.cluster_id


def test_refiner_skips_mic_source(two_voice_wav, two_voice_segments, tmp_path):
    store = SpeakerStore(tmp_path / "speakers.db")
    result = refine_loopback(
        two_voice_wav,
        two_voice_segments,
        speaker_store=store,
        encoder=FrequencyEmbedder(),
    )
    mic_label = next(lbl for lbl in result.segment_labels if lbl.segment_index == 1)
    assert mic_label.cluster_id is None
    assert mic_label.name is None


def test_refiner_matches_known_speaker(two_voice_wav, two_voice_segments, tmp_path):
    store = SpeakerStore(tmp_path / "speakers.db")
    # Seed the store with voice A's expected embedding (FrequencyEmbedder
    # puts 200Hz tones in the mid bucket -> (0, 1, 0)).
    store.upsert("Alice", _unit_vec(0.0, 1.0, 0.0))
    result = refine_loopback(
        two_voice_wav,
        two_voice_segments,
        speaker_store=store,
        encoder=FrequencyEmbedder(),
    )
    # Voice A cluster picks up "Alice".
    alice_cluster = next(c for c in result.clusters if c.name == "Alice")
    assert alice_cluster is not None
    # Voice B should still be unknown.
    unknown_ids = result.unknown_cluster_ids
    assert len(unknown_ids) == 1
    # Segment labels for voice A carry the name.
    label_a_first = next(
        lbl for lbl in result.segment_labels if lbl.segment_index == 0
    )
    assert label_a_first.name == "Alice"


def test_refiner_drops_clusters_with_no_segment_evidence(tmp_path):
    """A loopback can contain voiced turns that no transcript segment
    overlaps (e.g. short non-speech that VAD accepted but Whisper produced
    no text for). Those clusters should not appear in the result -- the
    speaker walker has nothing to show for them.
    """
    sr = 16000
    pcm = np.concatenate([
        _silence(0.6, sr),
        _sine(200, 2.0, sr),       # voice A, 0.6-2.6s -- covered by sys segment
        _silence(0.8, sr),
        _sine(500, 2.0, sr),       # voice B, 3.4-5.4s -- no sys segment overlaps
        _silence(0.8, sr),
        _sine(800, 2.0, sr),       # voice C, 6.2-8.2s -- no sys segment overlaps
    ])
    wav_path = tmp_path / "sys.wav"
    _write_wav(wav_path, pcm, sr)
    segments = [
        TranscriptSegment(source="sys", text="Only voice A is transcribed",
                          t_start=0.6, t_end=2.6),
    ]
    store = SpeakerStore(tmp_path / "speakers.db")
    result = refine_loopback(
        wav_path,
        segments,
        speaker_store=store,
        encoder=FrequencyEmbedder(),
    )
    # Refiner detected 3 voiced spans but only one had transcript coverage.
    assert len(result.clusters) == 1
    assert len(result.unknown_cluster_ids) == 1
    # The retained cluster is the one assigned to the sole sys segment.
    sole_label = next(lbl for lbl in result.segment_labels if lbl.segment_index == 0)
    assert sole_label.cluster_id == result.clusters[0].cluster_id


def test_refiner_drops_sub_one_second_turns_from_clustering(tmp_path):
    """Turns shorter than MIN_TURN_DURATION_FOR_CLUSTERING_SEC must not
    drive cluster assignments. Their transcript segments fall back to
    unlabeled (None cluster_id) -- the source-aware rewriter handles
    presentation.

    Without this filter, a 0.3s back-channel "yeah" produces a noisy
    embedding that the clusterer happily places near whichever
    long-form cluster's centroid happens to be closest, conflating two
    real speakers.
    """
    sr = 16000
    pcm = np.concatenate([
        _silence(0.5, sr),
        _sine(200, 2.0, sr),        # voice A long turn, 0.5-2.5s
        _silence(0.6, sr),
        _sine(500, 0.4, sr),        # short backchannel, 3.1-3.5s -- DROPPED
        _silence(0.6, sr),
        _sine(200, 2.0, sr),        # voice A long turn, 4.1-6.1s
    ])
    wav_path = tmp_path / "sys.wav"
    _write_wav(wav_path, pcm, sr)
    segments = [
        TranscriptSegment(source="sys", text="A speaks",     t_start=0.5, t_end=2.5),
        TranscriptSegment(source="sys", text="yeah",          t_start=3.1, t_end=3.5),
        TranscriptSegment(source="sys", text="A again",       t_start=4.1, t_end=6.1),
    ]
    store = SpeakerStore(tmp_path / "speakers.db")
    result = refine_loopback(
        wav_path,
        segments,
        speaker_store=store,
        encoder=FrequencyEmbedder(),
    )
    # Two long turns of the same voice -> exactly one cluster.
    assert len(result.clusters) == 1
    # The two long-form segments get the cluster; the short one is unlabeled.
    by_index = {lbl.segment_index: lbl for lbl in result.segment_labels}
    assert by_index[0].cluster_id == result.clusters[0].cluster_id
    assert by_index[2].cluster_id == result.clusters[0].cluster_id
    assert by_index[1].cluster_id is None


def test_refiner_returns_empty_when_only_sub_second_turns_present(tmp_path):
    """If every voiced turn is below the filter threshold, the refiner
    bails to the empty-result path rather than clustering on garbage.

    This is rare but possible -- a recording made of only short utterances
    (rapid fire backchannels, system audio prompts) shouldn't produce
    speaker attributions you can't trust.
    """
    sr = 16000
    pcm = np.concatenate([
        _silence(0.5, sr),
        _sine(200, 0.4, sr),
        _silence(0.6, sr),
        _sine(500, 0.4, sr),
        _silence(0.6, sr),
        _sine(800, 0.4, sr),
    ])
    wav_path = tmp_path / "sys.wav"
    _write_wav(wav_path, pcm, sr)
    segments = [
        TranscriptSegment(source="sys", text="ok",   t_start=0.5, t_end=0.9),
        TranscriptSegment(source="sys", text="yeah", t_start=1.5, t_end=1.9),
        TranscriptSegment(source="sys", text="mm",   t_start=2.5, t_end=2.9),
    ]
    store = SpeakerStore(tmp_path / "speakers.db")
    result = refine_loopback(
        wav_path,
        segments,
        speaker_store=store,
        encoder=FrequencyEmbedder(),
    )
    assert result.clusters == []
    assert result.unknown_cluster_ids == []
    assert all(lbl.cluster_id is None for lbl in result.segment_labels)


def test_refiner_empty_loopback_returns_no_labels(tmp_path):
    """Silent WAV -> no turns -> all segments unlabeled."""
    sr = 16000
    silent = _silence(3.0, sr)
    wav_path = tmp_path / "silent.wav"
    _write_wav(wav_path, silent, sr)
    segments = [
        TranscriptSegment(source="sys", text="Whisper", t_start=0.5, t_end=1.0),
    ]
    store = SpeakerStore(tmp_path / "speakers.db")
    result = refine_loopback(
        wav_path,
        segments,
        speaker_store=store,
        encoder=FrequencyEmbedder(),
    )
    assert result.clusters == []
    assert all(lbl.cluster_id is None for lbl in result.segment_labels)
    assert result.unknown_cluster_ids == []


def test_refiner_relabels_mic_bleed_to_sys_speaker(tmp_path):
    """A mic-source segment whose audio bled in from the loopback (and
    therefore matches the sys speaker's voiceprint, not the user's)
    should be relabeled with the sys speaker's name instead of the
    user's default mic label.
    """
    sr = 16000
    sys_pcm = np.concatenate([
        _silence(0.4, sr),
        _sine(500, 2.5, sr),       # voice B on loopback, 0.4-2.9s
    ])
    sys_wav = tmp_path / "sys.wav"
    _write_wav(sys_wav, sys_pcm, sr)

    # Mic.wav contains the SAME 500Hz tone (room mic picked up the
    # speakers playing voice B) -- the voiceprint check will fail and
    # the timing overlap will succeed, so this becomes a bleed.
    mic_pcm = np.concatenate([
        _silence(0.4, sr),
        _sine(500, 2.5, sr),
    ])
    mic_wav = tmp_path / "mic.wav"
    _write_wav(mic_wav, mic_pcm, sr)

    segments = [
        # Mic-source transcript line of Bob's words bleeding onto the user's mic.
        TranscriptSegment(source="mic", text="Bob bled onto the mic",
                          t_start=0.5, t_end=2.5),
        # Sys-source transcript line for the same speech on the loopback.
        TranscriptSegment(source="sys", text="Bob speaking",
                          t_start=0.5, t_end=2.5),
    ]
    store = SpeakerStore(tmp_path / "speakers.db")
    # User voiceprint is the 200Hz signature -- the (0,1,0) bucket from
    # FrequencyEmbedder. The mic bleed at 500Hz will land in (0,0,1) and
    # fail to match.
    user_voiceprint = _unit_vec(0.0, 1.0, 0.0)

    result = refine_loopback(
        sys_wav,
        segments,
        speaker_store=store,
        encoder=FrequencyEmbedder(),
        mic_wav=mic_wav,
        user_voiceprint=user_voiceprint,
        user_match_threshold=0.7,
    )

    mic_label = next(lbl for lbl in result.segment_labels if lbl.segment_index == 0)
    sys_label = next(lbl for lbl in result.segment_labels if lbl.segment_index == 1)
    # The mic-bleed segment should now share a cluster with the sys segment.
    assert mic_label.cluster_id is not None
    assert mic_label.cluster_id == sys_label.cluster_id


def test_refiner_keeps_user_mic_segments_unlabeled(tmp_path):
    """When a mic turn matches the user's voiceprint, the segment must
    stay unlabeled so the default mic->user rendering wins. Without
    this, the refiner would clobber genuine user speech with whichever
    sys cluster happens to overlap in time.
    """
    sr = 16000
    sys_pcm = np.concatenate([
        _silence(0.4, sr),
        _sine(500, 2.5, sr),       # voice B on loopback, 0.4-2.9s
    ])
    sys_wav = tmp_path / "sys.wav"
    _write_wav(sys_wav, sys_pcm, sr)
    # Mic.wav holds the user (200Hz) speaking at the same time as voice B.
    mic_pcm = np.concatenate([
        _silence(0.4, sr),
        _sine(200, 2.5, sr),
    ])
    mic_wav = tmp_path / "mic.wav"
    _write_wav(mic_wav, mic_pcm, sr)
    segments = [
        TranscriptSegment(source="mic", text="user replying",
                          t_start=0.5, t_end=2.5),
        TranscriptSegment(source="sys", text="voice B",
                          t_start=0.5, t_end=2.5),
    ]
    store = SpeakerStore(tmp_path / "speakers.db")
    # User voiceprint matches the mic (200Hz -> mid bucket).
    user_voiceprint = _unit_vec(0.0, 1.0, 0.0)

    result = refine_loopback(
        sys_wav,
        segments,
        speaker_store=store,
        encoder=FrequencyEmbedder(),
        mic_wav=mic_wav,
        user_voiceprint=user_voiceprint,
        user_match_threshold=0.7,
    )
    mic_label = next(lbl for lbl in result.segment_labels if lbl.segment_index == 0)
    # User mic stays unlabeled -> mic-default user rendering wins.
    assert mic_label.cluster_id is None


def test_refiner_no_voiceprint_preserves_legacy_mic_behavior(tmp_path):
    """Regression: when no voiceprint is provided, mic-source segments
    must not be touched by the refiner -- caller still owns the user
    attribution via the existing rewrite pipeline.
    """
    sr = 16000
    sys_pcm = np.concatenate([_silence(0.4, sr), _sine(500, 2.0, sr)])
    sys_wav = tmp_path / "sys.wav"
    _write_wav(sys_wav, sys_pcm, sr)
    mic_pcm = np.concatenate([_silence(0.4, sr), _sine(500, 2.0, sr)])
    mic_wav = tmp_path / "mic.wav"
    _write_wav(mic_wav, mic_pcm, sr)
    segments = [
        TranscriptSegment(source="mic", text="something",
                          t_start=0.5, t_end=2.3),
    ]
    store = SpeakerStore(tmp_path / "speakers.db")
    # No user_voiceprint passed -- bleed detection should be skipped.
    result = refine_loopback(
        sys_wav,
        segments,
        speaker_store=store,
        encoder=FrequencyEmbedder(),
        mic_wav=mic_wav,
    )
    mic_label = next(lbl for lbl in result.segment_labels if lbl.segment_index == 0)
    assert mic_label.cluster_id is None
    assert mic_label.name is None


def test_apply_labels_sets_speaker_name_on_matched_sys_segments():
    segments = [
        TranscriptSegment(source="sys", text="hi",  t_start=1.0, t_end=2.0),
        TranscriptSegment(source="mic", text="me",  t_start=2.0, t_end=2.5),
    ]
    labels = [
        SegmentLabel(segment_index=0, cluster_id=0, name="Alice", confidence=0.9),
        SegmentLabel(segment_index=1, cluster_id=None, name=None, confidence=None),
    ]
    rewritten = apply_labels_to_segments(segments, labels)
    assert rewritten[0].speaker_name == "Alice"
    # Source preserved.
    assert rewritten[0].source == "sys"
    # Mic segment untouched.
    assert rewritten[1].speaker_name is None
    assert rewritten[1].source == "mic"


def test_apply_labels_uses_fallback_for_unnamed_clusters():
    segments = [
        TranscriptSegment(source="sys", text="hi", t_start=1.0, t_end=2.0),
    ]
    labels = [
        SegmentLabel(segment_index=0, cluster_id=2, name=None, confidence=None),
    ]
    rewritten = apply_labels_to_segments(segments, labels)
    # cluster_id=2 -> "Speaker 3" (1-based for display).
    assert rewritten[0].speaker_name == "Speaker 3"


def test_apply_labels_works_on_simplenamespace_doubles():
    """The refiner accepts non-dataclass shapes (used in some integration tests)."""
    segments = [
        SimpleNamespace(
            source="sys", text="hi", t_start=1.0, t_end=2.0,
            is_provisional=False, speaker_name=None,
        ),
    ]
    labels = [SegmentLabel(segment_index=0, cluster_id=0, name="Alice", confidence=0.9)]
    rewritten = apply_labels_to_segments(segments, labels)
    assert rewritten[0].speaker_name == "Alice"
