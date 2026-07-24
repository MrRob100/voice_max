"""
Standalone RVC inference script — run under .rvc_venv, called by imitate.py via subprocess.
Usage: python rvc_worker.py <input_wav> <output_wav> <model_pth> <index_path> <f0_up_key>
"""
import sys

input_wav, output_wav, model_pth, index_path, f0_up_key = (
    sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5])
)

from rvc_python.infer import RVCInference

rvc = RVCInference(device="cpu")
rvc.load_model(model_pth, index_path=index_path)
rvc.f0_up_key = f0_up_key
rvc.infer_file(input=input_wav, output=output_wav)
