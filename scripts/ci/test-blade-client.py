#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later

from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, call, patch
from xml.etree import ElementTree

import dbus

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'pylib'), str(ROOT / 'daemon')]
from openrazer.client.devices.keyboard import RazerKeyboard


class BladeClientTests(unittest.TestCase):
    METHODS = ('getFanState', 'getFanRPM', 'getFanLimits', 'getFanConfig', 'getFanStatus', 'setFanAuto', 'setFanManual')
    SELECT_METHODS = ('getFanGroups', 'setFanManualFans', 'setFanAutoFans')

    def setUp(self):
        self.fan = Mock(spec=self.METHODS + self.SELECT_METHODS)
        self.misc = Mock()
        self.misc.getDeviceName.return_value = 'Razer Blade'
        self.misc.getDeviceType.return_value = 'keyboard'
        self.misc.getDriverVersion.return_value = '3.12.1'
        self.misc.getVidPid.return_value = (0x1532, 0x0256)
        self.misc.hasMatrix.return_value = False
        self.introspection = Mock()
        self.interfaces = {
            'org.freedesktop.DBus.Introspectable': self.introspection,
            'razer.device.misc': self.misc,
            'razer.device.fan': self.fan,
            'razer.device.lighting.brightness': Mock(),
            'razer.device.lighting.chroma': Mock(),
            'razer.device.lighting.logo': Mock(),
        }
        self.proxy = object()
        bus = Mock()
        bus.get_object.return_value = self.proxy
        bus_patch = patch('dbus.SessionBus', return_value=bus)
        self.addCleanup(bus_patch.stop)
        bus_patch.start()
        interface_patch = patch('dbus.Interface', side_effect=lambda _proxy, name: self.interfaces[name])
        self.addCleanup(interface_patch.stop)
        self.interface_factory = interface_patch.start()

    def device(self, methods=METHODS):
        root = ElementTree.Element('node')
        if methods is not None:
            interface = ElementTree.SubElement(root, 'interface', name='razer.device.fan')
            for method in methods:
                ElementTree.SubElement(interface, 'method', name=method)
        self.introspection.Introspect.return_value = ElementTree.tostring(root, encoding='unicode')
        return RazerKeyboard('TESTSERIAL')

    def selective_device(self):
        return self.device(self.METHODS + self.SELECT_METHODS)

    def test_complete_interface_enables_capability_without_fan_io(self):
        device = self.device()
        self.assertTrue(device.has('fan_control'))
        self.assertFalse(device.has('fan_select_control'))
        self.assertIn(call(self.proxy, 'razer.device.fan'), self.interface_factory.call_args_list)
        self.assertEqual(self.fan.mock_calls, [])

    def test_capability_requires_every_method(self):
        for missing in self.METHODS:
            with self.subTest(missing=missing):
                self.interface_factory.reset_mock()
                device = self.device(tuple(method for method in self.METHODS if method != missing))
                self.assertFalse(device.has('fan_control'))
                self.assertNotIn(call(self.proxy, 'razer.device.fan'), self.interface_factory.call_args_list)
                self.assertEqual(self.fan.mock_calls, [])

    def test_missing_or_empty_interface_keeps_controls_unavailable(self):
        for methods in (None, (), ('getFanState', 'getFanRPM', 'getFanLimits', 'setFanAuto', 'setFanManual')):
            with self.subTest(methods=methods):
                self.interface_factory.reset_mock()
                device = self.device(methods)
                self.assertFalse(device.has('fan_control'))
                self.assertNotIn(call(self.proxy, 'razer.device.fan'), self.interface_factory.call_args_list)
                for name in ('fan_state', 'fan_rpm', 'fan_limits', 'fan_config', 'fan_status'):
                    with self.assertRaises(NotImplementedError):
                        getattr(device, name)
                with self.assertRaises(NotImplementedError):
                    device.set_fan_auto()
                with self.assertRaises(NotImplementedError):
                    device.set_fan_manual(2900)
                self.assertEqual(self.fan.mock_calls, [])

    def test_selective_capability_requires_every_method(self):
        device = self.selective_device()
        self.assertTrue(device.has('fan_control'))
        self.assertTrue(device.has('fan_select_control'))
        self.assertEqual(self.fan.mock_calls, [])
        for missing in self.METHODS + self.SELECT_METHODS:
            with self.subTest(missing=missing):
                device = self.device(tuple(method for method in self.METHODS + self.SELECT_METHODS if method != missing))
                self.assertFalse(device.has('fan_select_control'))
                with self.assertRaises(NotImplementedError):
                    _ = device.fan_groups
                with self.assertRaises(NotImplementedError):
                    device.set_fan_manual_fans({1: 2900})
                with self.assertRaises(NotImplementedError):
                    device.set_fan_auto_fans((1,))

    def test_selective_groups_and_exact_targets(self):
        device = self.selective_device()
        self.fan.getFanGroups.return_value = dbus.Dictionary({dbus.String('cpu_gpu'): dbus.Array([dbus.Byte(1), dbus.Byte(2)], signature='y'),
                                                               dbus.String('battery'): dbus.Array([dbus.Byte(3), dbus.Byte(4)], signature='y')}, signature='say')
        groups = device.fan_groups
        self.assertEqual(groups, {'cpu_gpu': (1, 2), 'battery': (3, 4)})
        self.assertIs(type(groups), dict)
        self.assertTrue(all(type(name) is str and type(ids) is tuple and all(type(fan_id) is int for fan_id in ids)
                            for name, ids in groups.items()))
        device.set_fan_manual_fans({1: 2900, 2: 3000})
        device.set_fan_auto_fans((2, 1))
        self.assertEqual(self.fan.mock_calls, [call.getFanGroups(), call.setFanManualFans({1: 2900, 2: 3000}), call.setFanAutoFans([2, 1])])

    def test_group_methods_resolve_current_state_before_writing(self):
        device = self.selective_device()
        self.fan.getFanGroups.return_value = {'cpu_gpu': (1, 2), 'battery': (3, 4)}
        self.fan.getFanState.return_value = [(1, 0, 'auto', 0), (2, 0, 'auto', 0), (3, 0, 'auto', 0), (4, 0, 'auto', 0)]
        device.set_fan_group_manual('cpu_gpu', 2900)
        device.set_fan_group_auto('battery')
        self.assertEqual(self.fan.mock_calls, [call.getFanGroups(), call.getFanState(), call.setFanManualFans({1: 2900, 2: 2900}),
                                               call.getFanGroups(), call.getFanState(), call.setFanAutoFans([3, 4])])
        self.fan.reset_mock()
        self.fan.getFanState.return_value = [(1, 0, 'auto', 0)]
        with self.assertRaises(ValueError):
            device.set_fan_group_manual('cpu_gpu', 2900)
        with self.assertRaises(ValueError):
            device.set_fan_group_auto('battery')
        self.assertFalse(any(entry[0].startswith('setFan') for entry in self.fan.mock_calls))

    def test_invalid_selective_arguments_do_not_contact_daemon(self):
        device = self.selective_device()
        for targets in ({}, {0: 2900}, {1: 2350}, {True: 2900}, {1: True}, {'1': 2900}, {1: 25600}):
            with self.subTest(targets=targets), self.assertRaises(ValueError):
                device.set_fan_manual_fans(targets)
        for ids in ((), (0,), (1, 1), (True,), ('1',), (256,)):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                device.set_fan_auto_fans(ids)
        self.assertEqual(self.fan.mock_calls, [])

    def test_state_converts_dbus_values_to_native_types(self):
        device = self.device()
        self.fan.getFanState.return_value = dbus.Array([
            dbus.Struct((dbus.Byte(1), dbus.Byte(0), dbus.String('manual'), dbus.UInt16(2900))),
            dbus.Struct((dbus.Byte(2), dbus.Byte(6), dbus.String('auto'), dbus.UInt16(0))),
        ], signature='(yysq)')
        state = device.fan_state
        self.assertEqual(state, [(1, 0, 'manual', 2900), (2, 6, 'auto', 0)])
        self.assertIs(type(state), list)
        for fan in state:
            self.assertIs(type(fan), tuple)
            self.assertEqual(tuple(type(value) for value in fan), (int, int, str, int))
        self.assertEqual(self.fan.mock_calls, [call.getFanState()])

    def test_current_rpm_preserves_fan_ids_and_zero_values(self):
        device = self.device()
        self.fan.getFanRPM.return_value = dbus.Dictionary({dbus.Byte(1): dbus.UInt16(2800), dbus.Byte(4): dbus.UInt16(0)}, signature='yq')
        rpm = device.fan_rpm
        self.assertEqual(rpm, {1: 2800, 4: 0})
        self.assertIs(type(rpm), dict)
        for fan_id, current in rpm.items():
            self.assertIs(type(fan_id), int)
            self.assertIs(type(current), int)
        self.assertEqual(self.fan.mock_calls, [call.getFanRPM()])

    def test_limits_are_native_and_refreshed_each_time(self):
        device = self.device()
        self.fan.getFanLimits.side_effect = [
            (dbus.UInt16(2300), dbus.UInt16(2900), dbus.UInt16(4300)),
            (dbus.UInt16(2000), dbus.UInt16(3000), dbus.UInt16(5000)),
        ]
        limits = device.fan_limits
        self.assertEqual(limits, (2300, 2900, 4300))
        self.assertIs(type(limits), tuple)
        self.assertTrue(all(type(value) is int for value in limits))
        self.assertEqual(device.fan_limits, (2000, 3000, 5000))
        self.assertEqual(self.fan.mock_calls, [call.getFanLimits(), call.getFanLimits()])

    def test_config_decodes_registered_bits_to_native_tuples(self):
        device = self.device()
        self.fan.getFanConfig.return_value = (dbus.UInt32(0x71), dbus.UInt32(0x01), dbus.UInt32(0x06))
        config = device.fan_config
        self.assertEqual(config, {'automatic_modes': (0, 4, 5, 6), 'manual_modes': (0,), 'monitored_fans': (1, 2)})
        self.assertIs(type(config), dict)
        for key, values in config.items():
            self.assertIs(type(key), str)
            self.assertIs(type(values), tuple)
            self.assertTrue(all(type(value) is int for value in values))
        self.assertEqual(self.fan.mock_calls, [call.getFanConfig()])

    def test_config_does_not_hardcode_current_model_modes(self):
        device = self.device()
        self.fan.getFanConfig.return_value = (dbus.UInt32(0x80000009), dbus.UInt32(0x08), dbus.UInt32(0x80000002))
        self.assertEqual(device.fan_config, {'automatic_modes': (0, 3, 31), 'manual_modes': (3,), 'monitored_fans': (1, 31)})
        self.fan.getFanConfig.return_value = (dbus.UInt32(0x01), dbus.UInt32(0), dbus.UInt32(0x02))
        self.assertEqual(device.fan_config, {'automatic_modes': (0,), 'manual_modes': (), 'monitored_fans': (1,)})
        self.assertEqual(self.fan.mock_calls, [call.getFanConfig(), call.getFanConfig()])

    def test_config_rejects_invalid_masks(self):
        device = self.device()
        for invalid in (True, False, -1, 0x100000000, '1', 1.5):
            for index in range(3):
                with self.subTest(invalid=invalid, index=index):
                    values = [0x71, 0x01, 0x06]
                    values[index] = invalid
                    self.fan.getFanConfig.return_value = tuple(values)
                    with self.assertRaises(ValueError):
                        _ = device.fan_config

    def test_status_is_native_and_preserves_requested_vs_measured_speed(self):
        device = self.device()
        current = dbus.Dictionary({dbus.Byte(1): dbus.UInt16(2800), dbus.Byte(2): dbus.UInt16(2900), dbus.Byte(4): dbus.UInt16(0)}, signature='yq')
        self.fan.getFanStatus.return_value = (dbus.String('settling'), dbus.UInt16(2900), current, dbus.String(''))
        status = device.fan_status
        self.assertEqual(status, ('settling', 2900, {1: 2800, 2: 2900, 4: 0}, ''))
        self.assertIs(type(status), tuple)
        self.assertEqual(tuple(type(value) for value in status), (str, int, dict, str))
        self.assertTrue(all(type(key) is int and type(value) is int for key, value in status[2].items()))
        status[2][1] = 0
        self.assertEqual(current[1], 2800)
        self.assertEqual(self.fan.mock_calls, [call.getFanStatus()])

    def test_status_keeps_controller_phases_and_retained_target(self):
        device = self.device()
        for phase in ('idle', 'auto', 'suspended', 'settling', 'reached', 'timeout',
                      'partially_reached', 'partially_timeout', 'accepted', 'error', 'cancelled'):
            with self.subTest(phase=phase):
                self.fan.reset_mock()
                self.fan.getFanStatus.return_value = (dbus.String(phase), dbus.UInt16(2900), dbus.Dictionary({}, signature='yq'), dbus.String('Current reason'))
                self.assertEqual(device.fan_status, (phase, 2900, {}, 'Current reason'))
                self.assertEqual(self.fan.mock_calls, [call.getFanStatus()])

    def test_manual_forwards_exact_rpm_without_clamping_or_other_calls(self):
        device = self.device()
        for rpm in (100, 2300, 2900, 4300, 25500):
            with self.subTest(rpm=rpm):
                self.fan.reset_mock()
                device.set_fan_manual(rpm)
                self.assertEqual(self.fan.mock_calls, [call.setFanManual(rpm)])

    def test_invalid_manual_values_do_not_contact_daemon(self):
        device = self.device()
        for rpm in (True, False, None, '2900', 2900.0, -100, 0, 1, 2350, 25600, 65535):
            with self.subTest(rpm=rpm):
                with self.assertRaises(ValueError):
                    device.set_fan_manual(rpm)
        self.assertEqual(self.fan.mock_calls, [])

    def test_auto_has_no_rpm_argument_or_getter_dependency(self):
        device = self.device()
        for method in ('getFanState', 'getFanRPM', 'getFanLimits', 'getFanConfig', 'getFanStatus'):
            getattr(self.fan, method).side_effect = RuntimeError('Getter unavailable')
        device.set_fan_auto()
        self.assertEqual(self.fan.mock_calls, [call.setFanAuto()])

    def test_daemon_errors_propagate_without_retry(self):
        device = self.device()
        operations = (
            ('getFanState', lambda: device.fan_state),
            ('getFanRPM', lambda: device.fan_rpm),
            ('getFanLimits', lambda: device.fan_limits),
            ('getFanConfig', lambda: device.fan_config),
            ('getFanStatus', lambda: device.fan_status),
            ('setFanAuto', device.set_fan_auto),
            ('setFanManual', lambda: device.set_fan_manual(2900)),
        )
        for method, operation in operations:
            with self.subTest(method=method):
                self.fan.reset_mock(side_effect=True)
                error = dbus.DBusException('Fan operation failed')
                getattr(self.fan, method).side_effect = error
                with self.assertRaises(dbus.DBusException) as caught:
                    operation()
                self.assertIs(caught.exception, error)
                self.assertEqual(len(self.fan.mock_calls), 1)


if __name__ == '__main__':
    unittest.main()
