import os
import subprocess
import tempfile


def convert_to_opus_ogg(input_file, output_file=None, bitrate="32k", sample_rate=24000):
    if not os.path.isfile(input_file):
        raise FileNotFoundError(f"Input file not found: {input_file}")

    if output_file is None:
        output_file = os.path.splitext(input_file)[0] + ".ogg"

    output_dir = os.path.dirname(output_file)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir)

    cmd = [
        "ffmpeg", "-i", input_file,
        "-c:a", "libopus",
        "-b:a", bitrate,
        "-ar", str(sample_rate),
        "-application", "voip",
        "-vbr", "on",
        "-compression_level", "10",
        "-frame_duration", "60",
        "-y",
        output_file,
    ]

    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return output_file
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"FFmpeg conversion failed: {e.stderr}")


def convert_to_opus_ogg_temp(input_file, bitrate="32k", sample_rate=24000):
    tmp = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
    tmp.close()
    try:
        convert_to_opus_ogg(input_file, tmp.name, bitrate, sample_rate)
        return tmp.name
    except Exception:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)
        raise
