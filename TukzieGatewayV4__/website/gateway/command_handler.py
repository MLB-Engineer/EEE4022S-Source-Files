"""Gateway-side envelope adapter. Import into the existing MQTT subscriber.

This module has no GPIO assumptions. A successful acknowledgement is emitted
only after the supplied device callback confirms success. See readme.md.
"""
import json
import time
from collections import OrderedDict
from command_protocol import sign_envelope
import hmac

COMMANDS = {'hoot', 'left_indicator_on', 'left_indicator_off',
            'right_indicator_on', 'right_indicator_off', 'hazards_on', 'hazards_off', 'message'}


class CommandHandler:
    def __init__(self, secret, execute, max_age=10):
        self.secret = secret
        self.execute = execute  # callback(cmd, message) -> optional text
        self.max_age = max_age
        self.completed = OrderedDict()

    def handle(self, payload):
        """Return an ack dict, or None for an invalid/expired envelope.

        Authentication failures are deliberately silent. Business/hardware
        errors return a correlated 'rejected' ack. The callback must raise
        if a command could not actually be applied.
        """
        try:
            if not isinstance(payload, dict):
                return None
            expected = sign_envelope(payload, self.secret)
            if not hmac.compare_digest(expected, str(payload.get('auth_token_v2', ''))):
                return None
            timestamp = float(payload['timestamp'])
            age = time.time() - timestamp
            if not -2 <= age <= self.max_age:
                return None
            command_id = payload['command_id']
            cmd = payload['cmd']
            if not isinstance(command_id, str) or len(command_id) > 64 or not command_id:
                return None
            # QoS 1 can redeliver: never sound the hooter twice for one request.
            now = time.time()
            for key in list(self.completed):
                if now - self.completed[key]['processed_at'] > self.max_age + 5:
                    del self.completed[key]
            if command_id in self.completed:
                return self.completed[command_id]
            ack = {'command_id': command_id, 'cmd': cmd, 'processed_at': now}
            try:
                if cmd not in COMMANDS or payload.get('target') != 'vehicle':
                    raise ValueError('Unsupported command or target')
                message = payload.get('message')
                if cmd == 'message' and (not isinstance(message, str) or not message.strip() or len(message) > 280):
                    raise ValueError('Invalid message')
                # Reject overload rather than evict an unexpired ID and risk
                # executing a duplicate. Apply your own physical rate limits.
                if len(self.completed) >= 2000:
                    raise RuntimeError('Command handler is busy')
                note = self.execute(cmd, message)
                if note is False:
                    raise RuntimeError('Command was not applied')
                ack.update(status='ack', note=str(note or 'Applied by gateway'))
            except Exception as exc:
                ack.update(status='rejected', note=str(exc)[:500])
            if len(self.completed) < 2000:
                self.completed[command_id] = ack
            return ack
        except (KeyError, ValueError, TypeError):
            return None


def mqtt_callback(handler, client, msg, ack_topic='tukzie/acks'):
    """Call for the commands topic from your existing on_message callback."""
    if getattr(msg, 'retain', False):
        return
    try:
        ack = handler.handle(json.loads(msg.payload))
        if ack:
            client.publish(ack_topic, json.dumps(ack), qos=1, retain=False)
    except (ValueError, UnicodeDecodeError):
        return
