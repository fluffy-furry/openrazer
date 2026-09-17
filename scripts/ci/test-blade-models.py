#!/usr/bin/env python3

import configparser
import ctypes
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class Model(ctypes.Structure):
    _fields_ = [
        ("product_id", ctypes.c_ushort),
        ("interface_number", ctypes.c_ubyte),
        ("fan_profile", ctypes.c_ubyte),
        ("info_profile", ctypes.c_ubyte),
        ("features", ctypes.c_uint),
        ("manual_modes", ctypes.c_uint),
        ("automatic_modes", ctypes.c_uint),
        ("monitored_fans", ctypes.c_uint),
    ]


class BladeModelTests(unittest.TestCase):
    PRODUCTS = (0x0253, 0x0256, 0x026E, 0x0270, 0x028B, 0x029F, 0x02B8, 0x02C6)
    AUTOMATIC_MODES = {
        0x0253: {0, 4, 6},
        0x0256: {0, 4, 6},
        0x026E: {0, 4, 6},
        0x0270: {0, 4, 6},
        0x028B: {0, 4, 5, 6},
        0x029F: {0, 4, 5, 6, 7},
        0x02B8: {0, 1, 4, 5, 6, 7},
        0x02C6: {0, 2, 3, 4, 5, 6, 7},
    }

    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("gcc")
        if compiler is None:
            raise RuntimeError("gcc is required to test the actual C model catalog")
        cls.directory = tempfile.TemporaryDirectory(prefix="openrazer-blade-models-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(__file__).resolve().parents[2]
        library = Path(cls.directory.name) / "blade_models.so"
        subprocess.run([
            compiler, "-std=c99", "-Wall", "-Wextra", "-Werror", "-pedantic",
            "-fPIC", "-shared", str(cls.root / "driver" / "razerblade_models.c"),
            "-o", str(library),
        ], check=True)
        cls.library = ctypes.CDLL(str(library))
        cls.lookup = cls.library.razer_blade_lookup_model
        cls.lookup.argtypes = [ctypes.c_ushort, ctypes.c_ubyte]
        cls.lookup.restype = ctypes.POINTER(Model)
        cls.supports = cls.library.razer_blade_fan_mode_supported
        cls.supports.argtypes = [ctypes.POINTER(Model), ctypes.c_ubyte, ctypes.c_int]
        cls.supports.restype = ctypes.c_int

    def test_registered_family_entries(self):
        for product in self.PRODUCTS:
            with self.subTest(product=hex(product)):
                pointer = self.lookup(product, 2)
                self.assertTrue(pointer)
                model = pointer.contents
                self.assertEqual(model.product_id, product)
                self.assertEqual(model.interface_number, 2)
                self.assertEqual((model.fan_profile, model.info_profile), (1, 0))
                self.assertEqual(model.features, 3 if product in (0x029F, 0x02B8) else 1)
                self.assertEqual(model.manual_modes, 1 << 0)
                automatic = sum(1 << mode for mode in self.AUTOMATIC_MODES[product])
                self.assertEqual(model.automatic_modes, automatic)
                self.assertEqual(model.monitored_fans, (1 << 1) | (1 << 2))

    def test_unknown_products_and_other_interfaces_are_not_enabled(self):
        for product in (0, 0x0255, 0x02FF, 0xFFFF):
            with self.subTest(product=hex(product)):
                self.assertFalse(self.lookup(product, 2))
        for product in self.PRODUCTS:
            for interface in range(256):
                if interface != 2:
                    with self.subTest(product=hex(product), interface=interface):
                        self.assertFalse(self.lookup(product, interface))

    def test_entire_compiled_catalog_matches_registered_products(self):
        catalog = {product for product in range(65536) if self.lookup(product, 2)}
        self.assertEqual(catalog, set(self.PRODUCTS))

    def test_fake_devices_expose_fans_only_for_registered_products(self):
        catalog = {product for product in range(65536) if self.lookup(product, 2)}
        registered = set()
        expected = {"fan_control": "w", "fan_limits": "r", "fan_rpm": "r", "fan_state": "r",
                    "fan_modes": "r", "fan_rpm_monitor": "r"}
        for path in (self.root / "pylib/openrazer/_fake_driver").glob("*.cfg"):
            config = configparser.ConfigParser()
            config.read(path)
            identity = config["device"]["dir_name"].split(":")
            vendor = int(identity[1], 16)
            product = int(identity[2].split(".")[0], 16)
            attributes = {}
            values = {}
            for line in config["device"]["files"].splitlines():
                access, name, *value = line.split(",", 2)
                if name.startswith("fan_"):
                    attributes[name] = access
                    values[name] = value[0] if value else ""
            with self.subTest(device=path.name):
                if vendor == 0x1532 and product in catalog:
                    self.assertEqual(attributes, expected)
                    model = self.lookup(product, 2).contents
                    self.assertEqual(values["fan_modes"], f"{model.automatic_modes} {model.manual_modes}")
                    self.assertEqual(values["fan_rpm_monitor"], str(model.monitored_fans))
                    registered.add(product)
                else:
                    self.assertEqual(attributes, {})
        self.assertEqual(registered, catalog)

    def test_manual_and_automatic_modes_remain_distinct(self):
        for product in self.PRODUCTS:
            model = self.lookup(product, 2)
            automatic_modes = self.AUTOMATIC_MODES[product]
            for performance in range(32):
                with self.subTest(product=hex(product), performance=performance):
                    self.assertEqual(self.supports(model, performance, 1), int(performance == 0))
                    self.assertEqual(self.supports(model, performance, 0), int(performance in automatic_modes))

    def test_mask_helper_obeys_descriptor_and_rejects_out_of_range_shift(self):
        model = Model()
        model.manual_modes = (1 << 2) | (1 << 31)
        model.automatic_modes = (1 << 2) | (1 << 5)
        self.assertEqual(self.supports(ctypes.byref(model), 2, 1), 1)
        self.assertEqual(self.supports(ctypes.byref(model), 31, 1), 1)
        self.assertEqual(self.supports(ctypes.byref(model), 5, 1), 0)
        self.assertEqual(self.supports(ctypes.byref(model), 5, 0), 1)
        self.assertEqual(self.supports(ctypes.byref(model), 0, 0), 0)
        for performance in (32, 255):
            for manual in (0, 1):
                with self.subTest(performance=performance, manual=manual):
                    self.assertEqual(self.supports(ctypes.byref(model), performance, manual), 0)
        self.assertEqual(self.supports(None, 0, 0), 0)


if __name__ == "__main__":
    unittest.main()
