import hashlib
import hmac
import time


class SignatureError(ValueError):
    pass


def verify_signature(payload: bytes, header: str, secret: str, *, now: int | None = None, tolerance: int = 300) -> None:
    values: dict[str, list[str]] = {}
    for part in header.split(","):
        if "=" in part:
            key, value = part.split("=", 1)
            values.setdefault(key, []).append(value)
    try:
        timestamp = int(values["t"][0])
    except (KeyError, IndexError, ValueError) as exc:
        raise SignatureError("Missing or invalid timestamp") from exc
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > tolerance:
        raise SignatureError("Timestamp outside tolerance")
    signed = str(timestamp).encode() + b"." + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, signature) for signature in values.get("v1", [])):
        raise SignatureError("Signature mismatch")
