"""Platform-neutral single-session ownership and PCM endpoint detection."""
from array import array
import math
import sys
import uuid


class SessionGate:
    def __init__(self):
        self.active_id = None

    def begin(self):
        if self.active_id is not None:
            return None
        self.active_id = uuid.uuid4().hex
        return self.active_id

    def is_current(self, token):
        return token is not None and token == self.active_id

    def cancel(self):
        self.active_id = None

    def complete(self, token):
        if not self.is_current(token):
            return False
        self.cancel()
        return True


class Endpoint:
    """Energy endpoint used only for finite HTTP clips, not the realtime ASR VAD."""
    def __init__(self):
        self.voiced = 0
        self.quiet = 0

    def feed(self, pcm):
        samples = array('h')
        samples.frombytes(pcm[:len(pcm) // 2 * 2])
        if sys.byteorder != 'little':
            samples.byteswap()
        last_level = 0.0
        for start in range(0, len(samples), 160):
            block = samples[start:start + 160]
            last_level = math.sqrt(sum(value * value for value in block) / len(block)) / 32768
            if last_level > .012:
                self.voiced += len(block)
                self.quiet = 0
            elif self.voiced:
                self.quiet += len(block)
        return last_level

    @property
    def usable(self):
        return self.voiced >= 1600

    @property
    def ended(self):
        return self.voiced >= 3200 and self.quiet >= 10400
