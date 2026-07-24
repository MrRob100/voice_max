import os
import subprocess
import tempfile

import librosa
import numpy as np
import soundfile as sf
from pedalboard import Pedalboard, Reverb, Phaser, Chorus
from pedalboard.io import AudioFile

from audio import convert_to_opus_ogg

MUSIC_DIR = os.path.join(os.path.dirname(__file__), "..", "music")

# Available beats — add entries here as new tracks land in music/
BEATS = {
    "the_ghetto": {
        "file": "the_ghetto_instrumental.mp3",
        "start_offset": 22.0,   # skip the quiet melodic intro, drop in where beat kicks
    },
    "gin_juice": {
        "file": "gin_juice.aif",
        "start_offset": 0.0,
    },
    "mobb_deep": {
        "file": "mobb_deep_31s.mp3",
        "start_offset": 31.0,
    },
    "pools": {
        "file": "pools_25s.mp3",
        "start_offset": 25.0,
    },
    "where_my_mind": {
        "file": "where_my_mind_25s.mp3",
        "start_offset": 25.0,
    },
}

ACTIVE_BEAT = "the_ghetto"

# Global stretch tolerance — beyond this, skip bar-snapping to avoid warping short notes
MAX_GLOBAL_STRETCH = 0.45
# Per-segment stretch limits — loosened to pass more anchors and do more alignment work
MIN_SEGMENT_RATE = 0.60
MAX_SEGMENT_RATE = 1.7
# How far to pull each onset toward its beat target (0=no effect, 1=hard snap)
# 0.65 gives groove feel without mangling speech around it
SNAP_STRENGTH = 0.90


# ---------------------------------------------------------------------------
# Reverb — disabled, kept for future configurability
# ---------------------------------------------------------------------------

def apply_reverb(input_ogg: str, output_ogg: str, room_size: float = 0.75, wet_level: float = 0.5) -> None:
    wav_in = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    wav_out = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    try:
        subprocess.run(["ffmpeg", "-y", "-i", input_ogg, wav_in], check=True, capture_output=True)
        board = Pedalboard([Reverb(room_size=room_size, damping=0.3, wet_level=wet_level,
                                   dry_level=1.0 - wet_level, freeze_mode=0.0)])
        with AudioFile(wav_in) as f:
            audio = f.read(f.frames)
            sr = f.samplerate
            num_channels = f.num_channels
        effected = board(audio, sr)
        with AudioFile(wav_out, "w", samplerate=sr, num_channels=num_channels) as f:
            f.write(effected)
        convert_to_opus_ogg(wav_out, output_ogg)
    finally:
        for p in [wav_in, wav_out]:
            if os.path.exists(p):
                os.unlink(p)


# ---------------------------------------------------------------------------
# Phaser / modulation
# ---------------------------------------------------------------------------

def apply_phaser(
    input_ogg: str,
    output_ogg: str,
    rate_hz: float = 1.6,
    depth: float = 1.0,
    feedback: float = 0.75,
    mix: float = 1.0,
) -> None:
    """
    Heavy sweeping phaser plus a thick chorus for an obviously modulated,
    swirling voice.

    rate_hz:  LFO sweep speed — how fast the phaser swooshes.
    depth:    how much of the frequency range the sweep covers (1.0 = full).
    feedback: resonance of the notches — higher = more metallic/ringing.
    mix:      wet/dry blend of the phaser (1.0 = fully wet, strongest effect).
    """
    wav_in = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    wav_out = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    try:
        subprocess.run(["ffmpeg", "-y", "-i", input_ogg, wav_in], check=True, capture_output=True)
        board = Pedalboard([
            Phaser(rate_hz=rate_hz, depth=depth, centre_frequency_hz=1000.0,
                   feedback=feedback, mix=mix),
            Chorus(rate_hz=1.5, depth=0.6, centre_delay_ms=8.0, feedback=0.25, mix=0.5),
        ])
        with AudioFile(wav_in) as f:
            audio = f.read(f.frames)
            sr = f.samplerate
            num_channels = f.num_channels
        effected = board(audio, sr)
        with AudioFile(wav_out, "w", samplerate=sr, num_channels=num_channels) as f:
            f.write(effected)
        convert_to_opus_ogg(wav_out, output_ogg)
    finally:
        for p in [wav_in, wav_out]:
            if os.path.exists(p):
                os.unlink(p)


# ---------------------------------------------------------------------------
# Beat-aligned music bed
# ---------------------------------------------------------------------------

def _strong_onsets_hf(y: np.ndarray, sr: int, keep_top_pct: float = 0.4) -> tuple[np.ndarray, np.ndarray]:
    """
    Detect onset transients using high-frequency energy (2kHz+).
    Consonants (stops, fricatives) live here; vowels don't — so this fires
    specifically on the percussive attacks we want to align to the beat.
    Returns (onset_times, onset_strengths).
    """
    hf_strength = librosa.onset.onset_strength(y=y, sr=sr, fmin=2000.0)
    onset_frames = librosa.onset.onset_detect(onset_envelope=hf_strength, sr=sr, units="frames")

    if len(onset_frames) == 0:
        return np.array([]), np.array([])

    strengths = hf_strength[np.clip(onset_frames, 0, len(hf_strength) - 1)]
    threshold = np.percentile(strengths, (1.0 - keep_top_pct) * 100)
    strong_mask = strengths >= threshold

    times = librosa.frames_to_time(onset_frames[strong_mask], sr=sr)
    return times, strengths[strong_mask]


def _build_weighted_snap_grid(beat_times: np.ndarray) -> list[tuple[float, float]]:
    """
    Build snap targets with weights that reflect beat importance in 4/4:
      beat 1 & 3 (even indices) → weight 1.0  strong pull
      beat 2 & 4 (odd indices)  → weight 0.6  medium pull
      8th-note upbeats          → weight 0.3  weak pull
    """
    grid: list[tuple[float, float]] = []
    for i, t in enumerate(beat_times):
        weight = 1.0 if i % 2 == 0 else 0.6
        grid.append((float(t), weight))
        if i < len(beat_times) - 1:
            mid = (float(t) + float(beat_times[i + 1])) / 2.0
            grid.append((mid, 0.3))
    return grid


def _anchor_is_feasible(
    onset_t: float,
    target_t: float,
    prev: tuple[float, float],
    nxt: tuple[float, float],
) -> bool:
    """
    Check that inserting an anchor at (onset_t, target_t) keeps both neighbouring
    segments within [MIN_SEGMENT_RATE, MAX_SEGMENT_RATE].
    """
    for src_dur, tgt_dur in [
        (onset_t - prev[0], target_t - prev[1]),
        (nxt[0] - onset_t,  nxt[1] - target_t),
    ]:
        if tgt_dur <= 0:
            return False
        rate = src_dur / tgt_dur
        if not (MIN_SEGMENT_RATE <= rate <= MAX_SEGMENT_RATE):
            return False
    return True


def _build_warp_map(
    onset_times: np.ndarray,
    onset_strengths: np.ndarray,
    snap_grid: list[tuple[float, float]],
    voice_duration: float,
    target_duration: float,
) -> list[tuple[float, float]]:
    """
    Build monotonic (source_t, target_t) warp anchors.

    - Strongest onsets are assigned first (priority on downbeats).
    - Each onset is pulled SNAP_STRENGTH of the way toward its beat target
      rather than hard-snapped — preserves speech flow.
    - Each candidate anchor is feasibility-checked: if inserting it would
      force a neighbouring segment outside the rate limits, it is dropped.
      This prevents the bunching/gap problem caused by over-constraining.
    """
    # --- Step 1: assign onsets to beats, strongest first ---
    used_snap_indices: set[int] = set()
    candidates: list[tuple[float, float]] = []  # (onset_t, hard_snap_t)

    for i in np.argsort(onset_strengths)[::-1]:
        onset_t = float(onset_times[i])
        if onset_t < 0.05 or onset_t > voice_duration - 0.05:
            continue

        best_idx, best_score = None, float("inf")
        for idx, (snap_t, weight) in enumerate(snap_grid):
            if idx in used_snap_indices:
                continue
            score = abs(onset_t - snap_t) / weight
            if score < best_score:
                best_score, best_idx = score, idx

        if best_idx is not None:
            used_snap_indices.add(best_idx)
            candidates.append((onset_t, snap_grid[best_idx][0]))

    # --- Step 2: soft-snap and feasibility-check in time order ---
    candidates.sort(key=lambda x: x[0])
    end_anchor: tuple[float, float] = (voice_duration, target_duration)
    anchors: list[tuple[float, float]] = [(0.0, 0.0)]

    for onset_t, hard_snap_t in candidates:
        # pull SNAP_STRENGTH of the way toward the beat rather than going 100%
        soft_t = onset_t + SNAP_STRENGTH * (hard_snap_t - onset_t)

        # find the next already-committed anchor after this onset
        nxt = next((a for a in [end_anchor] if a[0] > onset_t), end_anchor)

        if (
            soft_t > anchors[-1][1]           # target time still advancing
            and onset_t > anchors[-1][0]       # source time still advancing
            and _anchor_is_feasible(onset_t, soft_t, anchors[-1], nxt)
        ):
            anchors.append((onset_t, soft_t))
            print(f"[effects]   anchor {onset_t:.3f}s → {soft_t:.3f}s "
                  f"(beat {hard_snap_t:.3f}s, pull {SNAP_STRENGTH*100:.0f}%)")

    anchors.append(end_anchor)

    # monotonicity safety pass
    result = [anchors[0]]
    for pt in anchors[1:]:
        if pt[0] > result[-1][0] and pt[1] > result[-1][1]:
            result.append(pt)
    return result


def _warp_segments(y: np.ndarray, sr: int, warp_map: list[tuple[float, float]]) -> np.ndarray:
    """
    Stretch each audio segment between consecutive warp anchors
    so that the output time matches the target anchor times.
    """
    segments = []
    for i in range(len(warp_map) - 1):
        src_s, tgt_s = warp_map[i]
        src_e, tgt_e = warp_map[i + 1]

        src_dur = src_e - src_s
        tgt_dur = tgt_e - tgt_s
        if src_dur <= 0 or tgt_dur <= 0:
            continue

        start = int(src_s * sr)
        end = int(src_e * sr)
        segment = y[start:end]

        if len(segment) < 64:
            segments.append(np.zeros(int(tgt_dur * sr)))
            continue

        rate = float(np.clip(src_dur / tgt_dur, MIN_SEGMENT_RATE, MAX_SEGMENT_RATE))
        stretched = librosa.effects.time_stretch(y=segment, rate=rate)

        # trim or pad to exact target sample count
        target_len = int(tgt_dur * sr)
        if len(stretched) >= target_len:
            stretched = stretched[:target_len]
        else:
            stretched = np.pad(stretched, (0, target_len - len(stretched)))

        segments.append(stretched)

    return np.concatenate(segments) if segments else y


def apply_music_bed(
    input_ogg: str,
    output_ogg: str,
    beat_name: str = None,
    instrumental_gain: float = 0.2,
) -> None:
    """
    Non-linearly warp the voice note so its strong onset transients (consonants,
    syllable attacks) land on beat positions of the instrumental, then mix.

    beat_name: key from BEATS dict (defaults to ACTIVE_BEAT).
    instrumental_gain: 0.2 ≈ -14dB — sits clearly under the voice.
    """
    beat = BEATS[beat_name or ACTIVE_BEAT]
    instrumental_path = os.path.join(MUSIC_DIR, beat["file"])
    start_offset = beat["start_offset"]

    wav_voice = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    wav_out = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name

    try:
        # --- decode voice OGG → WAV ---
        subprocess.run(["ffmpeg", "-y", "-i", input_ogg, wav_voice],
                       check=True, capture_output=True)

        y_voice, sr = librosa.load(wav_voice, sr=None, mono=True)
        voice_duration = len(y_voice) / sr

        # --- analyse instrumental for BPM and beat grid ---
        y_analysis, sr_instr = librosa.load(instrumental_path, sr=None, mono=True,
                                            offset=start_offset, duration=60)
        tempo, beat_frames = librosa.beat.beat_track(y=y_analysis, sr=sr_instr)
        bpm = float(np.atleast_1d(tempo)[0])
        beat_times = librosa.frames_to_time(beat_frames, sr=sr_instr)

        snap_grid = _build_weighted_snap_grid(beat_times)
        print(f"[effects] BPM: {bpm:.1f} | snap grid: {len(snap_grid)} positions (weighted by beat strength)")

        # --- snap overall duration to nearest bar boundary ---
        bar_duration = 4.0 * (60.0 / bpm)
        num_bars = max(1, round(voice_duration / bar_duration))
        target_duration = num_bars * bar_duration
        global_rate = voice_duration / target_duration

        if abs(global_rate - 1.0) > MAX_GLOBAL_STRETCH:
            print(f"[effects] global stretch {global_rate:.3f} exceeds limit — using natural duration")
            target_duration = voice_duration

        print(f"[effects] voice {voice_duration:.2f}s → {num_bars} bars → target {target_duration:.2f}s")

        # --- detect consonant-focused onsets via high-frequency band ---
        onset_times, onset_strengths = _strong_onsets_hf(y_voice, sr, keep_top_pct=0.4)
        print(f"[effects] {len(onset_times)} HF onsets — strongest assigned to downbeats first")

        # --- build warp map and apply non-linear stretching ---
        warp_map = _build_warp_map(onset_times, onset_strengths, snap_grid, voice_duration, target_duration)
        print(f"[effects] {len(warp_map)} warp anchors (including start/end)")
        y_warped = _warp_segments(y_voice, sr, warp_map)

        # --- load instrumental from offset, trimmed to target duration, at voice sample rate ---
        y_instr, _ = librosa.load(instrumental_path, sr=sr, mono=True,
                                  offset=start_offset, duration=target_duration)

        # align lengths (librosa rounding can differ by 1 sample)
        n = min(len(y_warped), len(y_instr))
        y_warped = y_warped[:n]
        y_instr = y_instr[:n]

        # --- mix and normalise ---
        y_mixed = y_warped + y_instr * instrumental_gain
        peak = np.max(np.abs(y_mixed))
        if peak > 1.0:
            y_mixed = y_mixed / peak

        # --- write and encode ---
        sf.write(wav_out, y_mixed, sr)
        convert_to_opus_ogg(wav_out, output_ogg)

    finally:
        for p in [wav_voice, wav_out]:
            if os.path.exists(p):
                os.unlink(p)
