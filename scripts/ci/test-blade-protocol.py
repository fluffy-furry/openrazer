#!/usr/bin/env python3

import ctypes
import errno
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


GPU_GET = bytes.fromhex("""
    00 00 00 00 00 02 0d 89 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 86 00
""")
GPU_SET_DISCRETE = bytes.fromhex("""
    00 0a 00 00 00 02 0d 09 00 01
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 07 00
""")
FAN_MANUAL = bytes.fromhex("""
    00 1e 00 00 00 04 0d 02 01 01
    00 01 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 0a 00
""")
FAN_4500_RPM = bytes.fromhex("""
    00 01 00 00 00 03 0d 01 01 01
    2d 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 22 00
""")
GPU_REPLY_DISCRETE = bytes.fromhex("""
    02 00 00 00 00 02 0d 89 02 01
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 85 00
""")


def byte_buffer(value):
    return (ctypes.c_ubyte * len(value)).from_buffer_copy(value)


def changed_reply(offset, value, repair_checksum=True):
    reply = bytearray(GPU_REPLY_DISCRETE)
    original = reply[offset]
    reply[offset] = value
    if repair_checksum and 2 <= offset < 88:
        reply[88] ^= original ^ value
    return reply


class BladeProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("gcc")
        if compiler is None:
            raise RuntimeError("gcc is required to test the actual C protocol helpers")
        cls.directory = tempfile.TemporaryDirectory(prefix="openrazer-blade-tests-")
        cls.addClassCleanup(cls.directory.cleanup)
        root = Path(__file__).resolve().parents[2]
        library = Path(cls.directory.name) / "blade_protocol.so"
        subprocess.run([
            compiler, "-std=c99", "-Wall", "-Wextra", "-Werror", "-pedantic",
            "-fPIC", "-shared", str(root / "driver" / "razerblade_protocol.c"),
            "-o", str(library),
        ], check=True)
        cls.library = ctypes.CDLL(str(library))
        pointer = ctypes.POINTER(ctypes.c_ubyte)
        cls.build = cls.library.razer_blade_build_request
        cls.build.argtypes = [pointer, ctypes.c_ubyte, ctypes.c_ubyte, pointer, ctypes.c_uint]
        cls.build.restype = ctypes.c_int
        cls.build_class = cls.library.razer_blade_build_request_class
        cls.build_class.argtypes = [pointer, ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ubyte, pointer, ctypes.c_uint]
        cls.build_class.restype = ctypes.c_int
        cls.validate = cls.library.razer_blade_validate_response
        cls.validate.argtypes = [pointer, pointer, ctypes.c_uint, ctypes.c_uint]
        cls.validate.restype = ctypes.c_int

    def check_reply(self, reply, expected, minimum=2, length=None, request=GPU_GET):
        if length is None:
            length = len(reply)
        self.assertEqual(self.validate(byte_buffer(request), byte_buffer(reply), length, minimum), expected)

    def test_exact_request_vectors(self):
        cases = (
            (0, 0x89, b"\x00\x00", GPU_GET),
            (10, 0x09, b"\x00\x01", GPU_SET_DISCRETE),
            (30, 0x02, b"\x01\x01\x00\x01", FAN_MANUAL),
            (1, 0x01, b"\x01\x01\x2d", FAN_4500_RPM),
        )
        for transaction, command, args, expected in cases:
            with self.subTest(command=command, transaction=transaction):
                self.assertEqual(len(expected), 90)
                output = byte_buffer(bytes([0xAA]) * 90)
                self.assertEqual(self.build(output, transaction, command, byte_buffer(args), len(args)), 0)
                self.assertEqual(bytes(output), expected)

    def test_maximum_fan_flag_query_uses_separate_class_and_bit(self):
        expected = bytes.fromhex('00 00 00 00 00 01 07 8f') + bytes(80) + bytes.fromhex('89 00')
        output = byte_buffer(bytes([0xAA]) * 90)
        self.assertEqual(self.build_class(output, 0, 7, 0x8F, byte_buffer(b'\0'), 1), 0)
        self.assertEqual(bytes(output), expected)
        reply = bytes.fromhex('02 00 00 00 00 01 07 8f 02') + bytes(79) + bytes.fromhex('8b 00')
        self.check_reply(reply, 0, minimum=1, request=expected)
        malformed = bytearray(reply)
        malformed[6] = 0x0D
        malformed[88] ^= 7 ^ 0x0D
        self.check_reply(malformed, -errno.EPROTO, minimum=1, request=expected)

    def test_maximum_fan_override_write_preserves_other_flag_bits(self):
        expected = bytes.fromhex('00 11 00 00 00 01 07 0f fd') + bytes(79) + bytes.fromhex('f4 00')
        output = byte_buffer(bytes([0xAA]) * 90)
        self.assertEqual(self.build_class(output, 17, 7, 0x0F, byte_buffer(b'\xfd'), 1), 0)
        self.assertEqual(bytes(output), expected)
        self.check_reply(b'\x02' + expected[1:], 0, minimum=1, request=expected)

    def test_builder_zero_and_maximum_payload(self):
        output = byte_buffer(bytes(90))
        self.assertEqual(self.build(output, 0, 0x80, None, 0), 0)
        self.assertEqual(bytes(output), bytes.fromhex("00 00 00 00 00 00 0d 80") + bytes(80) + b"\x8d\x00")
        args = byte_buffer(bytes([0xFF]) * 80)
        self.assertEqual(self.build(output, 30, 0x80, args, 80), 0)
        expected = bytes.fromhex("00 1e 00 00 00 50 0d 80") + bytes([0xFF]) * 80 + b"\xdd\x00"
        self.assertEqual(bytes(output), expected)

    def test_invalid_build_arguments_leave_output_untouched(self):
        args = byte_buffer(bytes(81))
        for transaction, source, size in ((31, args, 2), (255, args, 2), (0, args, 81), (0, args, 0xFFFFFFFF), (0, None, 1)):
            with self.subTest(transaction=transaction, size=size):
                output = byte_buffer(bytes([0xAA]) * 90)
                self.assertEqual(self.build(output, transaction, 0x09, source, size), -errno.EINVAL)
                self.assertEqual(bytes(output), bytes([0xAA]) * 90)
        self.assertEqual(self.build(None, 0, 0x09, args, 2), -errno.EINVAL)

    def test_successful_fixed_reply(self):
        self.assertEqual(len(GPU_REPLY_DISCRETE), 90)
        self.check_reply(GPU_REPLY_DISCRETE, 0)
        self.check_reply(GPU_REPLY_DISCRETE, 0, minimum=0)

    def test_captured_0256_thermal_reply_metadata_and_checksums(self):
        path = Path(__file__).parent / 'fixtures' / 'blade-0256-getter-capture.json'
        for packet in json.loads(path.read_text())['exchanges']:
            with self.subTest(query=packet['query']):
                request = bytes.fromhex(packet['request_hex'])
                response = bytes.fromhex(packet['response_hex'])
                self.assertNotEqual(request[2:4], response[2:4])
                self.check_reply(response, 0, minimum=packet['minimum_payload'], request=request)
                corrupted = bytearray(response)
                corrupted[3] ^= 1
                self.check_reply(corrupted, -errno.EPROTO, minimum=packet['minimum_payload'], request=request)

    def test_successful_maximum_payload(self):
        request = bytes.fromhex("00 1e 00 00 00 50 0d 80") + bytes([0xFF]) * 80 + b"\xdd\x00"
        response = b"\x02" + request[1:]
        self.check_reply(response, 0, minimum=80, request=request)

    def test_response_length_is_exact_without_reading_short_buffer(self):
        for length in (0, 1, 5, 89, 91, 0xFFFFFFFF):
            with self.subTest(length=length):
                self.check_reply(b"\x02", -errno.EPROTO, length=length)

    def test_oversized_payload_and_short_success(self):
        self.check_reply(changed_reply(5, 81), -errno.EPROTO)
        self.check_reply(changed_reply(5, 255), -errno.EPROTO)
        self.check_reply(changed_reply(5, 1), -errno.EPROTO)
        self.check_reply(changed_reply(5, 0), -errno.EPROTO)
        self.check_reply(GPU_REPLY_DISCRETE, -errno.EPROTO, minimum=3)

    def test_matching_headers_even_when_checksum_is_correct(self):
        for offset in (1, 4, 6, 7):
            with self.subTest(offset=offset):
                self.check_reply(changed_reply(offset, GPU_REPLY_DISCRETE[offset] ^ 1), -errno.EPROTO)

    def test_checksum_includes_entire_argument_area(self):
        for offset in (8, 9, 10, 87, 88):
            with self.subTest(offset=offset):
                self.check_reply(changed_reply(offset, GPU_REPLY_DISCRETE[offset] ^ 1, False), -errno.EPROTO)

    def test_firmware_status_errors(self):
        cases = ((1, errno.EBUSY), (3, errno.EIO), (4, errno.ETIMEDOUT), (5, errno.EOPNOTSUPP))
        for status, error in cases:
            with self.subTest(status=status):
                self.check_reply(changed_reply(0, status), -error)
                short = changed_reply(5, 0)
                short[0] = status
                self.check_reply(short, -error)
        for status in (0, 6, 225, 255):
            with self.subTest(status=status):
                self.check_reply(changed_reply(0, status), -errno.EPROTO)

    def test_busy_packet_still_requires_matching_header_and_checksum(self):
        for offset in (1, 7, 88):
            with self.subTest(offset=offset):
                reply = changed_reply(offset, GPU_REPLY_DISCRETE[offset] ^ 1)
                reply[0] = 1
                self.check_reply(reply, -errno.EPROTO)

    def test_invalid_validation_arguments(self):
        request = byte_buffer(GPU_GET)
        response = byte_buffer(GPU_REPLY_DISCRETE)
        self.assertEqual(self.validate(None, response, 90, 2), -errno.EINVAL)
        self.assertEqual(self.validate(request, None, 90, 2), -errno.EINVAL)
        for minimum in (81, 0xFFFFFFFF):
            with self.subTest(minimum=minimum):
                self.assertEqual(self.validate(request, response, 90, minimum), -errno.EINVAL)


if __name__ == "__main__":
    unittest.main()
