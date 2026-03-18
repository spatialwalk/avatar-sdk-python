import sys
import types
import unittest
from unittest.mock import patch

from avatarkit.audio_encoder import OggOpusStreamEncoder


class _FakeOpusError(Exception):
    pass


class _FakeEncoder:
    def __init__(self, sample_rate, channels, application):
        self.sample_rate = sample_rate
        self.channels = channels
        self.application = application

    @property
    def bitrate(self):
        return None

    @bitrate.setter
    def bitrate(self, value):
        raise _FakeOpusError(f"invalid bitrate: {value}")


class TestAudioEncoder(unittest.TestCase):
    def test_create_encoder_logs_warning_when_bitrate_ctl_fails(self):
        fake_opuslib = types.ModuleType("opuslib")
        fake_opuslib.Encoder = _FakeEncoder
        fake_opuslib.exceptions = types.SimpleNamespace(OpusError=_FakeOpusError)

        with (
            patch.dict(sys.modules, {"opuslib": fake_opuslib}),
            self.assertLogs("avatarkit.audio_encoder", level="WARNING") as logs,
        ):
            encoder = OggOpusStreamEncoder._create_encoder(24000, 32000, "audio")

        self.assertIsInstance(encoder, _FakeEncoder)
        self.assertIn("Failed to set Opus encoder bitrate", logs.output[0])
