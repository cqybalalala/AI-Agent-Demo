"""
STT client — calls the persistent VibeMouse STT server via HTTP.
Server must be running: VibeMouse/.venv/Scripts/python stt_server.py
"""

import httpx

STT_URL = "http://127.0.0.1:5555/transcribe"


def check_server() -> bool:
    """Returns True if the STT server is reachable."""
    try:
        r = httpx.post(STT_URL, content=b"", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def transcribe_bytes(audio_bytes: bytes) -> str:
    """POST raw audio bytes to the STT server, return transcript."""
    r = httpx.post(STT_URL, content=audio_bytes, timeout=15)
    r.raise_for_status()
    return r.text.strip()
