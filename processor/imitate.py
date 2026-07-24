import os
import subprocess
import tempfile

from audio import convert_to_opus_ogg

VOICES_DIR = os.path.join(os.path.dirname(__file__), "..", "voice_models")
RVC_PYTHON = os.path.join(os.path.dirname(__file__), ".rvc_venv", "bin", "python")
RVC_WORKER = os.path.join(os.path.dirname(__file__), "rvc_worker.py")

VOICES = {
    "kanye": {
        "model": "dondaera200/dondaera_e200_s7600.pth",
        "index": "dondaera200/added_IVF1056_Flat_nprobe_1_dondaera_v2.index",
    },
    "homer": {
        "model": "homero/homero.pth",
        "index": "homero/added_IVF113_Flat_nprobe_1_homero_v2.index",
    },
}


def apply_imitate(input_ogg: str, output_ogg: str, voice_name: str = "kanye", f0_up_key: int = 0) -> None:
    """
    Convert the voice note to sound like the target voice using RVC.
    No transcription — the original speech audio is converted directly.

    voice_name: key from VOICES dict.
    f0_up_key:  pitch shift in semitones. 0 if voices are similar pitch.
                Negative = lower, positive = higher.
    """
    if voice_name not in VOICES:
        raise ValueError(f"Unknown voice '{voice_name}'. Available: {list(VOICES.keys())}")

    voice = VOICES[voice_name]
    model_path = os.path.join(VOICES_DIR, voice["model"])
    index_path = os.path.join(VOICES_DIR, voice["index"])

    wav_in = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    wav_out = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_ogg, "-ar", "40000", "-ac", "1", wav_in],
            check=True, capture_output=True,
        )

        print(f"[imitate] converting voice → {voice_name} (f0_up_key={f0_up_key})…")
        result = subprocess.run(
            [RVC_PYTHON, RVC_WORKER, wav_in, wav_out, model_path, index_path, str(f0_up_key)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"RVC worker failed:\n{result.stderr}")
        print(f"[imitate] conversion done")

        convert_to_opus_ogg(wav_out, output_ogg)

    finally:
        for p in [wav_in, wav_out]:
            if os.path.exists(p):
                os.unlink(p)
