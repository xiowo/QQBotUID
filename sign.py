import json
from typing import Any, Dict, Union, Optional

from cryptography.hazmat.primitives.asymmetric import ed25519


def generate_signature(bot_secret: str, event_ts: str, plain_token: str) -> Dict[str, str]:
    raw = (bot_secret * ((32 // len(bot_secret)) + 1))[:32]
    key = ed25519.Ed25519PrivateKey.from_private_bytes(raw.encode())
    sig = key.sign(f"{event_ts}{plain_token}".encode()).hex()
    return {"plain_token": plain_token, "signature": sig}


def verify_and_respond(body: Union[bytes, str, Dict[str, Any]], bot_secret: str) -> Optional[Dict[str, str]]:
    data = json.loads(body) if isinstance(body, (bytes, str)) else body
    d = data.get("d") or {}
    token = d.get("plain_token")
    ts = d.get("event_ts")
    if not token or ts is None:
        return None
    return generate_signature(bot_secret, str(ts), str(token))
