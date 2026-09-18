#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later

import configparser
import errno
import gc
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'daemon'))

import dbus
from openrazer_daemon.dbus_services.dbus_methods import fan
from openrazer_daemon.dbus_services.service import DBusService
from openrazer_daemon.daemon import RazerDaemon
from openrazer_daemon.hardware.device_base import RazerDevice
from openrazer_daemon.hardware import keyboards
from openrazer_daemon.misc.fan_control import FanControl
from openrazer_daemon.misc.fan_monitor import FanMonitor


FAN_METHODS = ('get_fan_state', 'get_fan_rpm', 'get_fan_limits', 'get_fan_config', 'get_fan_status', 'set_fan_auto', 'set_fan_manual')
FAN_FILES = ('fan_state', 'fan_rpm', 'fan_limits', 'fan_control', 'fan_modes', 'fan_rpm_monitor')
FAN_SELECT_METHODS = ('get_fan_groups', 'set_fan_manual_fans', 'set_fan_auto_fans')
FAN_SELECT_FILES = FAN_FILES + ('fan_groups', 'fan_control_select')
FAN_MODELS = {0x0253, 0x0256, 0x026E, 0x0270, 0x028B, 0x029F, 0x02B8, 0x02C6}


class BladePersistenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='openrazer-fan-persistence-')
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'persistence.conf'
        self.daemon = SimpleNamespace(logger=Mock(), _persistence=configparser.ConfigParser())
        fan_device = SimpleNamespace(storage_name='BLADE', _fan_control=object(), METHODS=['set_fan_manual', 'set_dpi_xy'],
                                     dpi=(800, 900), ZONES={}, zone={})
        plain_device = SimpleNamespace(storage_name='PLAIN', _fan_control=None, METHODS=[], dpi=(0, 0), ZONES={}, zone={})
        self.daemon._razer_devices = [SimpleNamespace(dbus=fan_device), SimpleNamespace(dbus=plain_device)]

    def save_and_read(self):
        RazerDaemon.write_persistence(self.daemon, str(self.path))
        loaded = configparser.ConfigParser()
        loaded.read(self.path)
        return loaded

    def test_saved_manual_and_auto_survive_daemon_persistence_rebuild(self):
        self.daemon._persistence.read_dict({'BLADE': {'fan_mode': 'manual', 'fan_rpm': '2900', 'fan_performance_mode': '0', 'old_effect': 'stale'},
                                            'PLAIN': {'fan_mode': 'manual', 'fan_rpm': '2900', 'fan_performance_mode': '0'}})
        loaded = self.save_and_read()
        self.assertEqual(dict(loaded['BLADE']), {'fan_mode': 'manual', 'fan_rpm': '2900', 'fan_performance_mode': '0', 'dpi_x': '800', 'dpi_y': '900'})
        self.assertEqual(dict(loaded['PLAIN']), {})

        self.daemon._persistence.set('BLADE', 'fan_mode', 'auto')
        loaded = self.save_and_read()
        self.assertEqual(dict(loaded['BLADE']), {'fan_mode': 'auto', 'dpi_x': '800', 'dpi_y': '900'})

    def test_incomplete_or_invalid_manual_preference_is_not_saved(self):
        for values in ({'fan_mode': 'manual', 'fan_rpm': '2900'},
                       {'fan_mode': 'manual', 'fan_rpm': '2950', 'fan_performance_mode': '0'},
                       {'fan_mode': 'manual', 'fan_rpm': '2900', 'fan_performance_mode': '32'}):
            with self.subTest(values=values):
                self.daemon._persistence.remove_section('BLADE')
                self.daemon._persistence.read_dict({'BLADE': values})
                loaded = self.save_and_read()
                self.assertFalse(loaded.has_option('BLADE', 'fan_mode'))

    def test_selected_preference_survives_only_on_registered_models(self):
        selected = {'fan_mode': 'selective', 'fan_targets': '3:3000,1:2900', 'fan_performance_mode': '0'}
        self.daemon._persistence.read_dict({'BLADE': selected, 'PLAIN': selected})
        loaded = self.save_and_read()
        self.assertFalse(loaded.has_option('BLADE', 'fan_mode'))
        self.assertFalse(loaded.has_option('PLAIN', 'fan_mode'))
        self.daemon._razer_devices[0].dbus.METHODS.append('set_fan_manual_fans')
        self.daemon._persistence.read_dict({'BLADE': selected})
        loaded = self.save_and_read()
        self.assertEqual(loaded.get('BLADE', 'fan_mode'), 'selective')
        self.assertEqual(loaded.get('BLADE', 'fan_targets'), '1:2900,3:3000')
        self.assertEqual(loaded.get('BLADE', 'fan_performance_mode'), '0')
        self.assertFalse(loaded.has_option('PLAIN', 'fan_mode'))

    def test_invalid_selected_preference_is_not_saved(self):
        self.daemon._razer_devices[0].dbus.METHODS.append('set_fan_manual_fans')
        for targets in ('', '1:2900,1:3000', '0:2900', '1:2950', '1:25600', '1:-100', '1:2900,'):
            with self.subTest(targets=targets):
                self.daemon._persistence.remove_section('BLADE')
                self.daemon._persistence.read_dict({'BLADE': {'fan_mode': 'selective', 'fan_targets': targets, 'fan_performance_mode': '0'}})
                loaded = self.save_and_read()
                self.assertFalse(loaded.has_option('BLADE', 'fan_mode'))


class _FanDevice(RazerDevice):
    USB_VID = 0x1532
    USB_PID = 0x0256
    METHODS = list(FAN_METHODS)


class _SelectiveFanDevice(_FanDevice):
    METHODS = list(FAN_METHODS + FAN_SELECT_METHODS)


class _PlainDevice(RazerDevice):
    USB_VID = 0x1532
    USB_PID = 0x026F
    METHODS = ['get_device_type_keyboard']


class BladeDaemonTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='openrazer-fan-daemon-')
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.values = {
            'fan_state': '1 0 auto 0\n2 0 auto 0\n',
            'fan_rpm': '1 2800\n2 0\n',
            'fan_limits': '2300 2900 4300\n',
            'fan_control': 'untouched',
            'fan_control_select': 'untouched',
            'fan_groups': 'cpu_gpu 1,2\nbattery 3,4\n',
            'fan_modes': '81 1\n',
            'fan_rpm_monitor': '6\n',
            'device_serial': 'FANTEST123',
        }
        for filename, value in self.values.items():
            (self.path / filename).write_text(value)
        monitor_patch = patch('openrazer_daemon.misc.fan_control.FanMonitor', side_effect=self.make_monitor)
        self.monitor_factory = monitor_patch.start()
        self.addCleanup(monitor_patch.stop)
        config = configparser.ConfigParser()
        config.read_dict({'Startup': {'restore_persistence': 'false'}})
        self.device = SimpleNamespace(get_driver_path=lambda name: str(self.path / name), logger=Mock(), config=config,
                                      persistence=configparser.ConfigParser(), storage_name='FANTEST123')
        self.power = SimpleNamespace(power='ac', register=Mock(side_effect=lambda controller: controller.update_power('ac')), unregister=Mock())
        self.device._fan_control = FanControl(self.device)
        self.device._fan_control.attach_power(self.power)
        self.addCleanup(self.device._fan_control.close)

    @staticmethod
    def make_monitor(*_args):
        monitor = Mock(spec=FanMonitor)
        monitor.status = ('idle', 0, {}, '')

        def start(target, _ids, _modes):
            monitor.status = ('settling', target, {}, '')

        def cancel(reason):
            phase, target, speeds, _ = monitor.status
            if phase != 'idle':
                monitor.status = ('cancelled', target, speeds, reason)

        monitor.start.side_effect = start
        monitor.cancel.side_effect = cancel
        return monitor

    def assert_control_unchanged(self):
        self.assertEqual((self.path / 'fan_control').read_text(), 'untouched')

    def test_endpoint_signatures(self):
        signatures = {
            'get_fan_state': ('getFanState', None, 'a(yysq)'),
            'get_fan_rpm': ('getFanRPM', None, 'a{yq}'),
            'get_fan_limits': ('getFanLimits', None, 'qqq'),
            'get_fan_config': ('getFanConfig', None, 'uuu'),
            'get_fan_status': ('getFanStatus', None, 'sqa{yq}s'),
            'set_fan_auto': ('setFanAuto', None, None),
            'set_fan_manual': ('setFanManual', 'q', None),
        }
        for name, signature in signatures.items():
            method = getattr(fan, name)
            self.assertEqual(method.interface, 'razer.device.fan')
            self.assertEqual((method.name, method.in_sig, method.out_sig), signature)
            self.assertEqual(method.required_files, FAN_FILES)

        selective = {
            'get_fan_groups': ('getFanGroups', None, 'a{say}'),
            'set_fan_manual_fans': ('setFanManualFans', 'a{yq}', None),
            'set_fan_auto_fans': ('setFanAutoFans', 'ay', None),
        }
        for name, signature in selective.items():
            method = getattr(fan, name)
            self.assertEqual(method.interface, 'razer.device.fan')
            self.assertEqual((method.name, method.in_sig, method.out_sig), signature)
            self.assertEqual(method.required_files, FAN_SELECT_FILES)

    def test_selective_getter_and_setters_preserve_ids_and_targets(self):
        self.assertEqual(fan.get_fan_groups(self.device), {'cpu_gpu': [1, 2], 'battery': [3, 4]})
        with patch.object(self.device, '_fan_control') as control:
            fan.set_fan_manual_fans(self.device, {dbus.Byte(1): dbus.UInt16(2900), dbus.Byte(2): dbus.UInt16(3000)})
            control.set_manual_fans.assert_called_once_with({1: 2900, 2: 3000})
            sent = control.set_manual_fans.call_args.args[0]
            self.assertTrue(all(type(fan_id) is int and type(rpm) is int for fan_id, rpm in sent.items()))
            fan.set_fan_auto_fans(self.device, dbus.Array([dbus.Byte(2), dbus.Byte(1)], signature='y'))
            control.set_auto_fans.assert_called_once_with((2, 1))
            self.assertTrue(all(type(fan_id) is int for fan_id in control.set_auto_fans.call_args.args[0]))
        self.assertEqual((self.path / 'fan_control_select').read_text(), 'untouched')

    def test_selective_methods_reject_invalid_arguments_without_controller_io(self):
        with patch.object(self.device, '_fan_control') as control:
            for targets in ({}, {0: 2900}, {1: 2350}, {True: 2900}, {1: True}, {'1': 2900}, {1: 25600}):
                with self.subTest(targets=targets), self.assertRaises(ValueError):
                    fan.set_fan_manual_fans(self.device, targets)
            for ids in ((), (0,), (1, 1), (True,), ('1',), (256,)):
                with self.subTest(ids=ids), self.assertRaises(ValueError):
                    fan.set_fan_auto_fans(self.device, ids)
            control.assert_not_called()
            control.set_manual_fans.assert_not_called()
            control.set_auto_fans.assert_not_called()

    def test_selective_group_reader_rejects_corrupt_or_duplicate_ids(self):
        for value in ('', 'cpu_gpu\n', 'cpu_gpu 1,1\n', 'cpu_gpu 1,2\nbattery 2,4\n', 'CPU 1,2\n', 'cpu_gpu 0,2\n'):
            with self.subTest(value=value):
                (self.path / 'fan_groups').write_text(value)
                with self.assertRaises(ValueError):
                    fan.get_fan_groups(self.device)

    def test_selective_methods_require_both_model_registration_and_driver_files(self):
        connection = Mock()
        connection.list_exported_child_objects.return_value = []
        plain = self.make_device()
        self.assertNotIn('getFanGroups', plain.Introspect('/org/razer/device/FANTEST123', connection))
        for missing in ('fan_groups', 'fan_control_select'):
            with self.subTest(missing=missing):
                path = self.path / missing
                value = path.read_text()
                path.unlink()
                selective = self.make_device(model=_SelectiveFanDevice)
                self.assertNotIn('getFanGroups', selective.Introspect('/org/razer/device/FANTEST123', connection))
                path.write_text(value)
        selective = self.make_device(model=_SelectiveFanDevice)
        xml = selective.Introspect('/org/razer/device/FANTEST123', connection)
        for method in ('getFanGroups', 'setFanManualFans', 'setFanAutoFans'):
            self.assertIn(method, xml)

    def test_getters_return_distinct_targets_and_current_speed(self):
        (self.path / 'fan_state').write_text('1 0 manual 2900\n2 6 auto 0\n')
        self.assertEqual(fan.get_fan_state(self.device), [(1, 0, 'manual', 2900), (2, 6, 'auto', 0)])
        self.assertEqual(fan.get_fan_rpm(self.device), {1: 2800, 2: 0})
        self.assertEqual(fan.get_fan_limits(self.device), (2300, 2900, 4300))
        self.assertEqual(fan.get_fan_config(self.device), (81, 1, 6))
        self.assert_control_unchanged()

    def test_auto_does_not_read_limits_state_or_rpm(self):
        for filename in FAN_FILES:
            if filename != 'fan_control':
                (self.path / filename).unlink()
        fan.set_fan_auto(self.device)
        self.assertEqual((self.path / 'fan_control').read_bytes(), b'auto')

    def test_manual_writes_exact_requested_rpm(self):
        for rpm in (2300, 2900, 4300, dbus.UInt16(3000)):
            with self.subTest(rpm=rpm):
                fan.set_fan_manual(self.device, rpm)
                self.assertEqual((self.path / 'fan_control').read_bytes(), str(rpm).encode('ascii'))

    def test_manual_rejects_invalid_types_without_reading_or_writing(self):
        for rpm in (True, False, 2900.0, '2900', b'2900', None, [], {}):
            with self.subTest(rpm=rpm), patch('builtins.open', side_effect=AssertionError('Unexpected driver access')):
                with self.assertRaises(TypeError):
                    fan.set_fan_manual(self.device, rpm)
        self.assert_control_unchanged()

    def test_manual_rejects_invalid_steps_and_protocol_range_without_io(self):
        for rpm in (-100, 0, 2350, 25501, 25600, 65536):
            with self.subTest(rpm=rpm), patch('builtins.open', side_effect=AssertionError('Unexpected driver access')):
                with self.assertRaises(ValueError):
                    fan.set_fan_manual(self.device, rpm)
        self.assert_control_unchanged()

    def test_manual_rejects_values_outside_current_limits_without_clamping(self):
        for rpm in (2200, 4400):
            with self.subTest(rpm=rpm), self.assertRaises(ValueError):
                fan.set_fan_manual(self.device, rpm)
        self.assert_control_unchanged()

    def test_manual_uses_runtime_limits(self):
        (self.path / 'fan_limits').write_text('3000 4000 5000\n')
        with self.assertRaises(ValueError):
            fan.set_fan_manual(self.device, 2900)
        self.assert_control_unchanged()
        fan.set_fan_manual(self.device, 5000)
        self.assertEqual((self.path / 'fan_control').read_text(), '5000')

    def test_malformed_state_is_rejected(self):
        values = ('', '\n', '1 0 auto\n', '1 0 auto 0 extra\n', '1 0 unknown 2900\n',
                  '1 0 manual 0\n', '1 0 manual 2950\n', '0 0 auto 0\n', '256 0 auto 0\n',
                  '1 256 auto 0\n', '1 -1 auto 0\n', '1 0 auto -100\n', '1 0 auto 25600\n',
                  '1 0 auto 0\n1 0 auto 0\n', '١ 0 auto 0\n', '1 0 auto +0\n')
        for value in values:
            with self.subTest(value=value):
                (self.path / 'fan_state').write_text(value)
                with self.assertRaises(ValueError):
                    fan.get_fan_state(self.device)

    def test_malformed_current_speed_is_rejected(self):
        values = ('', '1\n', '1 2900 extra\n', '0 2900\n', '256 2900\n', '1 -100\n',
                  '1 25600\n', '1 2950\n', '1 2900\n1 2800\n', '1 nan\n')
        for value in values:
            with self.subTest(value=value):
                (self.path / 'fan_rpm').write_text(value)
                with self.assertRaises(ValueError):
                    fan.get_fan_rpm(self.device)

    def test_malformed_limits_block_manual_control(self):
        values = ('', '2300 2900\n', '2300 2900 4300 4500\n', '2300 2900 4300\n2300 2900 4300\n',
                  '0 2900 4300\n', '3000 2900 4300\n', '2300 4400 4300\n', '2300 2900 25600\n',
                  '2300 2950 4300\n', '-100 2900 4300\n', 'nan 2900 4300\n')
        for value in values:
            with self.subTest(value=value):
                (self.path / 'fan_limits').write_text(value)
                with self.assertRaises(ValueError):
                    fan.set_fan_manual(self.device, 2900)
        self.assert_control_unchanged()

    def test_malformed_configuration_blocks_manual_control(self):
        malformed = {
            'fan_modes': ('', '81\n', '81 1 6\n', '81 1\n81 1\n', '0 0\n', '1 2\n', '-1 1\n', '4294967296 1\n', '81 +1\n', '٨١ 1\n'),
            'fan_rpm_monitor': ('', '6 2\n', '6\n6\n', '0\n', '1\n', '7\n', '-6\n', '4294967296\n', '0x06\n', 'nan\n'),
        }
        for filename, values in malformed.items():
            for value in values:
                with self.subTest(filename=filename, value=value):
                    (self.path / filename).write_text(value)
                    with self.assertRaises(ValueError):
                        fan.get_fan_config(self.device)
                    with self.assertRaises(ValueError):
                        fan.set_fan_manual(self.device, 2900)
                    self.assert_control_unchanged()
            (self.path / filename).write_text(self.values[filename])

    def test_status_returns_cached_values_without_reading_device(self):
        with patch('builtins.open', side_effect=AssertionError('Status must not access hardware')):
            self.assertEqual(fan.get_fan_status(self.device), ('idle', 0, {}, ''))
        fan.set_fan_manual(self.device, 2900)
        monitor = self.device._fan_control._monitor
        monitor.status = ('settling', 2900, {1: 2800, 2: 2900}, '')
        with patch('builtins.open', side_effect=AssertionError('Status must not access hardware')):
            status = fan.get_fan_status(self.device)
            self.assertEqual(status, ('settling', 2900, {1: 2800, 2: 2900}, ''))
            status[2][1] = 0
            self.assertEqual(fan.get_fan_status(self.device)[2][1], 2800)

    def test_read_errors_propagate_without_writes(self):
        for name in ('get_fan_state', 'get_fan_rpm', 'get_fan_limits', 'get_fan_config', 'set_fan_manual'):
            args = (2900,) if name == 'set_fan_manual' else ()
            for error in (errno.EIO, errno.EOPNOTSUPP, errno.EPROTO, errno.ENOENT):
                with self.subTest(method=name, error=error), patch('builtins.open', side_effect=OSError(error, 'read failed')):
                    with self.assertRaises(OSError) as raised:
                        getattr(fan, name)(self.device, *args)
                    self.assertEqual(raised.exception.errno, error)
        self.assert_control_unchanged()

    def test_control_errors_propagate_without_retry(self):
        for method, args in ((fan.set_fan_auto, ()), (fan.set_fan_manual, (2900,))):
            for error in (errno.EIO, errno.EBUSY, errno.EAGAIN, errno.ERANGE, errno.EOPNOTSUPP):
                with self.subTest(method=method.__name__, error=error), patch.object(self.device._fan_control, '_write', side_effect=OSError(error, 'write failed')) as write:
                    with self.assertRaises(OSError) as raised:
                        method(self.device, *args)
                    self.assertEqual(raised.exception.errno, error)
                    expected = ['2900', 'auto'] if method is fan.set_fan_manual else ['auto']
                    self.assertEqual([call.args[0] for call in write.call_args_list], expected)
        self.assert_control_unchanged()

    def test_only_registered_models_expose_all_fan_methods(self):
        registered = set()
        for model in vars(keyboards).values():
            if not isinstance(model, type) or not hasattr(model, 'USB_PID'):
                continue
            actual = set(model.METHODS).intersection(FAN_METHODS)
            if actual:
                self.assertEqual(actual, set(FAN_METHODS), model.__name__)
                registered.add(model.USB_PID)
        self.assertEqual(registered, FAN_MODELS)

    def load_methods(self, methods):
        device = SimpleNamespace(
            METHODS=methods, methods_internal=[], get_driver_path=self.device.get_driver_path,
            logger=self.device.logger, add_dbus_method=Mock())
        with patch('builtins.open', side_effect=AssertionError('Registration must not access hardware')):
            RazerDevice.load_methods(device)
        return [call.args[1] for call in device.add_dbus_method.call_args_list]

    def test_registration_requires_every_file_but_does_not_read_them(self):
        expected = {'getFanState', 'getFanRPM', 'getFanLimits', 'getFanConfig', 'getFanStatus', 'setFanAuto', 'setFanManual'}
        self.assertEqual(set(self.load_methods(FAN_METHODS)), expected)
        for name in FAN_FILES:
            with self.subTest(missing=name):
                path = self.path / name
                path.unlink()
                self.assertEqual(self.load_methods(FAN_METHODS), [])
                path.write_text(self.values[name])
        self.assertEqual(self.load_methods(['get_device_type_keyboard']), ['getDeviceType'])

    def make_device(self, model=_FanDevice, cleanup=True):
        config = configparser.ConfigParser()
        config.read_dict({'Startup': {'restore_persistence': 'false', 'persistence_dual_boot_quirk': 'true'}})
        persistence = configparser.ConfigParser()
        persistence.read_dict({'FANTEST123': {'fan_mode': 'manual', 'fan_rpm': '4300'}})
        with patch('dbus.SessionBus'), patch('dbus.service.BusName.__new__', return_value=Mock()), patch.object(DBusService, 'add_to_connection'):
            device = model(str(self.path), 0, config, persistence, True, [], [], {})
        if cleanup:
            self.addCleanup(device.close)
        return device

    def test_optional_methods_are_isolated_for_same_model_instances_and_reconnects(self):
        connection = Mock()
        connection.list_exported_child_objects.return_value = []
        for availability in ((True, False, True), (False, True, False)):
            devices = []
            for present in availability:
                path = self.path / 'fan_control'
                if present:
                    path.write_text('untouched')
                else:
                    path.unlink(missing_ok=True)
                device = self.make_device()
                devices.append((device, present))
                for current, supported in devices:
                    with self.subTest(availability=availability, present=present, supported=supported):
                        xml = current.Introspect('/org/razer/device/FANTEST123', connection)
                        self.assertEqual('razer.device.fan' in xml, supported)
                        self.assertEqual(current._fan_control is not None, supported)
                        if supported:
                            method, _ = dbus.service._method_lookup(current, 'setFanAuto', 'razer.device.fan')
                            self.assertEqual(method.__name__, 'setFanAuto')
                        else:
                            with self.assertRaises(dbus.exceptions.UnknownMethodException):
                                dbus.service._method_lookup(current, 'setFanAuto', 'razer.device.fan')
                self.assertIsInstance(device, _FanDevice)
            self.assertEqual(len({type(device) for device, _ in devices}), 3)
        base_key = _FanDevice.__module__ + '.' + _FanDevice.__name__
        self.assertNotIn('razer.device.fan', getattr(_FanDevice, '_dbus_class_table')[base_key])
        self.assertNotIn('setFanAuto', _FanDevice.__dict__)

    def test_private_method_table_is_cleaned_up_with_device(self):
        device = self.make_device(cleanup=False)
        key = device.__class__.__module__ + '.' + device.__class__.__name__
        class_table = getattr(DBusService, '_dbus_class_table')
        self.assertIn(key, class_table)
        device.close()
        del device
        self.monitor_factory.reset_mock()
        gc.collect()
        self.assertNotIn(key, class_table)

    def test_models_without_optional_methods_keep_existing_class(self):
        device = self.make_device(model=_PlainDevice)
        self.assertIs(type(device), _PlainDevice)

    def test_startup_screensaver_resume_and_close_do_not_access_fans(self):
        real_open = open

        def checked_open(path, *args, **kwargs):
            if Path(path).name in FAN_FILES:
                raise AssertionError('Lifecycle must not access fans')
            return real_open(path, *args, **kwargs)

        with patch('builtins.open', side_effect=checked_open):
            device = self.make_device()
            device.configure_fan_control(self.power)
            device.suspend_device()
            device.resume_device()
            device.close()
        self.assert_control_unchanged()


if __name__ == '__main__':
    unittest.main()
