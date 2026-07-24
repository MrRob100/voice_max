import os
import tempfile

import httpx
import effects
from fastapi import FastAPI, Request

from effects import apply_reverb, apply_music_bed, apply_phaser
from imitate import apply_imitate, VOICES

app = FastAPI()

BRIDGE_URL = os.getenv("BRIDGE_URL", "http://localhost:8080")

config = {
    "mode": "reverb",        # "reverb", "beat", "imitate", or "phaser"
    "beat": "the_ghetto",
    "snap_strength": 0.90,
    "gain": 0.2,
    "voice": "kanye",        # key from imitate.VOICES
    "f0_up_key": 0,          # semitone pitch shift for RVC
}


@app.get("/beats")
async def list_beats():
    return {"beats": list(effects.BEATS.keys())}


@app.get("/voices")
async def list_voices():
    return {"voices": list(VOICES.keys())}


@app.get("/config")
async def get_config():
    return config


@app.post("/config")
async def set_config(request: Request):
    payload = await request.json()
    for key in ("mode", "beat", "snap_strength", "gain", "voice", "f0_up_key"):
        if key in payload:
            config[key] = payload[key]
    print(f"[processor] config updated: {config}")
    return {"success": True, "config": config}


@app.post("/process")
async def process_voice_note(request: Request):
    payload = await request.json()
    message_id = payload.get("message_id")
    chat_jid = payload.get("chat_jid")

    if not message_id or not chat_jid:
        return {"success": False, "error": "missing message_id or chat_jid"}

    async with httpx.AsyncClient(timeout=30) as client:
        dl = await client.post(f"{BRIDGE_URL}/api/download", json={
            "message_id": message_id,
            "chat_jid": chat_jid,
        })

    dl_data = dl.json()
    if not dl_data.get("success"):
        print(f"[processor] download failed: {dl_data.get('error')}")
        return {"success": False, "error": dl_data.get("error")}

    input_path = dl_data["path"]
    output_fd, output_path = tempfile.mkstemp(suffix=".ogg")
    os.close(output_fd)

    mode = config["mode"]
    try:
        if mode == "beat":
            effects.SNAP_STRENGTH = config["snap_strength"]
            apply_music_bed(input_path, output_path,
                            beat_name=config["beat"],
                            instrumental_gain=config["gain"])
            print(f"[processor] beat mode applied ({config['beat']}) → {output_path}")
        elif mode == "imitate":
            apply_imitate(input_path, output_path,
                          voice_name=config["voice"],
                          f0_up_key=config["f0_up_key"])
            print(f"[processor] imitate mode applied ({config['voice']}) → {output_path}")
        elif mode == "phaser":
            apply_phaser(input_path, output_path)
            print(f"[processor] phaser mode applied → {output_path}")
        else:
            apply_reverb(input_path, output_path)
            print(f"[processor] reverb applied → {output_path}")
    except Exception as e:
        os.unlink(output_path)
        print(f"[processor] processing failed: {e}")
        return {"success": False, "error": str(e)}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(f"{BRIDGE_URL}/api/revoke", json={
                "message_id": message_id,
                "chat_jid": chat_jid,
            })

        async with httpx.AsyncClient(timeout=30) as client:
            send = await client.post(f"{BRIDGE_URL}/api/send", json={
                "recipient": chat_jid,
                "audio_path": output_path,
            })

        send_data = send.json()
        if not send_data.get("success"):
            print(f"[processor] send failed: {send_data.get('error')}")
            return {"success": False, "error": send_data.get("error")}

        print(f"[processor] processed voice note sent to {chat_jid}")
        return {"success": True}

    finally:
        if os.path.exists(output_path):
            os.unlink(output_path)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "9000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
