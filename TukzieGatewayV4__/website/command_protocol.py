"""Shared authenticated envelope for website and gateway commands."""
import hashlib
import hmac
import json

SIGNING_FIELDS = ('cmd', 'target', 'timestamp', 'command_id', 'message')


def sign_envelope(payload, secret):
    body = {key: payload[key] for key in SIGNING_FIELDS if key in payload}
    canonical = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
