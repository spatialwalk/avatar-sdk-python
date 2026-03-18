"""Helpers for optional client-side Ogg Opus encoding."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import secrets
import struct
from typing import Optional


logger = logging.getLogger(__name__)


@dataclass
class EncodedAudioChunk:
    payload: bytes
    completed_stream: Optional[bytes] = None


class OggOpusStreamEncoder:
    _ALLOWED_SAMPLE_RATES = {8000, 12000, 16000, 24000, 48000}
    _ALLOWED_FRAME_DURATIONS_MS = {10, 20, 40, 60}
    _APPLICATIONS = {"audio", "voip", "restricted_lowdelay"}
    _DEFAULT_PRE_SKIP = 312
    _VENDOR = b"avatarkit"
    _CRC_POLY = 0x04C11DB7

    def __init__(
        self,
        *,
        sample_rate: int,
        bitrate: int,
        frame_duration_ms: int,
        application: str,
        collect_encoded_output: bool,
    ) -> None:
        self._validate_config(sample_rate, frame_duration_ms, application)

        self._sample_rate = sample_rate
        self._frame_duration_ms = frame_duration_ms
        self._application = application
        self._frame_size = sample_rate * frame_duration_ms // 1000
        self._frame_bytes = self._frame_size * 2
        self._sample_scale = 48000 // sample_rate
        self._pre_skip = self._DEFAULT_PRE_SKIP
        self._pcm_buffer = bytearray()
        self._pending_packet: Optional[bytes] = None
        self._pending_granule = 0
        self._headers_emitted = False
        self._page_sequence = 0
        self._stream_serial = secrets.randbits(32)
        self._total_input_samples = 0
        self._encoded_output: Optional[bytearray] = (
            bytearray() if collect_encoded_output else None
        )
        self._encoder = self._create_encoder(sample_rate, bitrate, application)

    def encode(self, pcm_data: bytes, *, end: bool) -> EncodedAudioChunk:
        if len(pcm_data) % 2 != 0:
            raise ValueError(
                "PCM input for internal Ogg Opus encoder must be 16-bit aligned"
            )

        if pcm_data:
            self._pcm_buffer.extend(pcm_data)

        payload = bytearray()
        self._encode_full_frames(payload)

        if end:
            self._flush_final_frame(payload)
            self._finalize_stream(payload)

        completed_stream = None
        if end and self._encoded_output:
            completed_stream = bytes(self._encoded_output)

        return EncodedAudioChunk(
            payload=bytes(payload), completed_stream=completed_stream
        )

    def _encode_full_frames(self, payload: bytearray) -> None:
        while len(self._pcm_buffer) >= self._frame_bytes:
            frame = bytes(self._pcm_buffer[: self._frame_bytes])
            del self._pcm_buffer[: self._frame_bytes]

            self._queue_audio_packet(payload, frame, self._frame_size)

    def _flush_final_frame(self, payload: bytearray) -> None:
        if not self._pcm_buffer:
            return

        actual_samples = len(self._pcm_buffer) // 2
        frame = bytes(self._pcm_buffer)
        frame += b"\x00" * (self._frame_bytes - len(frame))
        self._pcm_buffer.clear()

        self._queue_audio_packet(payload, frame, actual_samples)

    def _queue_audio_packet(
        self, payload: bytearray, pcm_frame: bytes, actual_samples: int
    ) -> None:
        if not self._headers_emitted:
            self._emit_headers(payload)

        packet = self._encoder.encode(pcm_frame, self._frame_size)
        self._total_input_samples += actual_samples
        granule = self._pre_skip + self._total_input_samples * self._sample_scale

        if self._pending_packet is not None:
            self._write_page(payload, self._pending_packet, self._pending_granule)

        self._pending_packet = packet
        self._pending_granule = granule

    def _finalize_stream(self, payload: bytearray) -> None:
        if self._pending_packet is not None:
            self._write_page(
                payload,
                self._pending_packet,
                self._pending_granule,
                end_of_stream=True,
            )
            self._pending_packet = None
            return

        if self._headers_emitted:
            self._write_page(payload, b"", self._pre_skip, end_of_stream=True)

    def _emit_headers(self, payload: bytearray) -> None:
        self._headers_emitted = True
        self._write_page(payload, self._build_opus_head(), 0, begin_of_stream=True)
        self._write_page(payload, self._build_opus_tags(), 0)

    def _write_page(
        self,
        payload: bytearray,
        packet: bytes,
        granule_position: int,
        *,
        begin_of_stream: bool = False,
        end_of_stream: bool = False,
    ) -> None:
        page = self._build_ogg_page(
            packet,
            granule_position,
            begin_of_stream=begin_of_stream,
            end_of_stream=end_of_stream,
        )
        payload.extend(page)
        if self._encoded_output is not None:
            self._encoded_output.extend(page)

    def _build_ogg_page(
        self,
        packet: bytes,
        granule_position: int,
        *,
        begin_of_stream: bool = False,
        end_of_stream: bool = False,
    ) -> bytes:
        header_type = 0
        if begin_of_stream:
            header_type |= 0x02
        if end_of_stream:
            header_type |= 0x04

        lacing_values = self._build_lacing_values(packet)
        header = bytearray()
        header.extend(b"OggS")
        header.append(0)
        header.append(header_type)
        header.extend(struct.pack("<Q", granule_position))
        header.extend(struct.pack("<I", self._stream_serial))
        header.extend(struct.pack("<I", self._page_sequence))
        header.extend(b"\x00\x00\x00\x00")
        header.append(len(lacing_values))
        header.extend(lacing_values)

        page = bytes(header) + packet
        checksum = self._ogg_crc(page)

        header[22:26] = struct.pack("<I", checksum)
        self._page_sequence += 1

        return bytes(header) + packet

    def _build_opus_head(self) -> bytes:
        packet = bytearray()
        packet.extend(b"OpusHead")
        packet.append(1)
        packet.append(1)
        packet.extend(struct.pack("<H", self._pre_skip))
        packet.extend(struct.pack("<I", self._sample_rate))
        packet.extend(struct.pack("<h", 0))
        packet.append(0)

        return bytes(packet)

    def _build_opus_tags(self) -> bytes:
        packet = bytearray()
        packet.extend(b"OpusTags")
        packet.extend(struct.pack("<I", len(self._VENDOR)))
        packet.extend(self._VENDOR)
        packet.extend(struct.pack("<I", 0))

        return bytes(packet)

    @classmethod
    def _build_lacing_values(cls, packet: bytes) -> bytes:
        if not packet:
            return b""

        size = len(packet)
        segments = bytearray()
        while size >= 255:
            segments.append(255)
            size -= 255

        segments.append(size)
        if len(packet) % 255 == 0:
            segments.append(0)

        return bytes(segments)

    @classmethod
    def _validate_config(
        cls, sample_rate: int, frame_duration_ms: int, application: str
    ) -> None:
        if sample_rate not in cls._ALLOWED_SAMPLE_RATES:
            raise ValueError(
                "Internal Ogg Opus encoder supports sample rates: "
                + ", ".join(str(rate) for rate in sorted(cls._ALLOWED_SAMPLE_RATES))
            )

        if frame_duration_ms not in cls._ALLOWED_FRAME_DURATIONS_MS:
            raise ValueError(
                "Internal Ogg Opus encoder supports frame durations: "
                + ", ".join(
                    str(duration)
                    for duration in sorted(cls._ALLOWED_FRAME_DURATIONS_MS)
                )
                + " ms"
            )

        if application not in cls._APPLICATIONS:
            raise ValueError(
                "Internal Ogg Opus encoder application must be one of: "
                + ", ".join(sorted(cls._APPLICATIONS))
            )

    @staticmethod
    def _create_encoder(sample_rate: int, bitrate: int, application: str):
        try:
            import opuslib
        except ImportError as exc:  # pragma: no cover - exercised by runtime users
            raise RuntimeError(
                "Internal Ogg Opus encoding requires the optional opus dependency. "
                "Install avatarkit[opus] to enable it."
            ) from exc

        encoder = opuslib.Encoder(sample_rate, 1, application)
        if bitrate > 0:
            try:
                encoder.bitrate = bitrate
            except opuslib.exceptions.OpusError as exc:
                logger.warning(
                    "Failed to set Opus encoder bitrate; using encoder default instead",
                    extra={"bitrate": bitrate},
                    exc_info=exc,
                )

        return encoder

    @classmethod
    def _ogg_crc(cls, data: bytes) -> int:
        crc = 0
        for byte in data:
            crc ^= byte << 24
            for _ in range(8):
                if crc & 0x80000000:
                    crc = ((crc << 1) ^ cls._CRC_POLY) & 0xFFFFFFFF
                else:
                    crc = (crc << 1) & 0xFFFFFFFF

        return crc
