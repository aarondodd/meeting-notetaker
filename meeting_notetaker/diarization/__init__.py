"""Speaker identification + persistence.

Post-meeting refinement pipeline that runs after the batch-transcribe pass:

    segmenter.find_turns(loopback_wav)
        -> list of (start, end) spans where someone in the room was talking

    embeddings.VoiceEncoder.embed_segment(pcm)
        -> fixed-length speaker embedding (ECAPA-TDNN; 192-dim float32)

    cluster.cluster_segments(embeddings)
        -> assigns each segment to an anonymous cluster id

    store.SpeakerStore.match(centroid)
        -> looks up a known name by cosine similarity against stored centroids

    refiner.refine_transcript(...)
        -> orchestrates the above and rewrites raw.transcript.md with names
           where confidence >= threshold; returns the unlabeled clusters so
           the UI can prompt the user.

Only `embeddings` requires SpeechBrain (and therefore torch + torchaudio).
The other modules are pure-numpy/stdlib so they can be unit-tested without
the heavy dep installed.
"""
from __future__ import annotations

from .cluster import ClusterAssignment, cluster_segments  # noqa: F401
from .segmenter import Turn, find_turns  # noqa: F401
from .store import SpeakerRecord, SpeakerStore  # noqa: F401
