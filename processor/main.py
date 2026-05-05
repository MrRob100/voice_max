import os
import tempfile

import httpx
from fastapi import FastAPI, Request

from effects import apply_reverb
from effects import apply_music_bed  # beat-aligned music bed

app = FastAPI()

BRIDGE_URL = os.getenv("BRIDGE_URL", "http://localhost:8080")


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

    try:
        # apply_reverb(input_path, output_path)
        apply_music_bed(input_path, output_path)
        print(f"[processor] reverb applied → {output_path}")
    except Exception as e:
        os.unlink(output_path)
        print(f"[processor] reverb failed: {e}")
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
