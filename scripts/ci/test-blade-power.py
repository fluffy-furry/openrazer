#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later

import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'daemon'))

from openrazer_daemon.misc.fan_power import FanPowerMonitor


class FanPowerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='openrazer-fan-power-')
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.glib = Mock()
        self.glib.timeout_add_seconds.return_value = 42
        patcher = patch('openrazer_daemon.misc.fan_power.GLib', self.glib)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.monitor = FanPowerMonitor(logging.getLogger('fan-power-test'), self.path, testing=True)
        self.addCleanup(self.monitor.close)

    def supply(self, name, **values):
        supply = self.path / name
        supply.mkdir(exist_ok=True)
        for key, value in values.items():
            (supply / key).write_text(value, encoding='ascii')
        return supply

    def test_empty_and_device_scoped_supplies_are_unknown(self):
        self.assertEqual(self.monitor.power, 'unknown')
        self.supply('mouse', scope='Device', type='Battery', present='1')
        self.supply('charger', scope='Device', type='USB', online='1')
        self.assertEqual(self.monitor.power, 'unknown')

    def test_ac_and_battery_follow_fresh_online_values(self):
        self.supply('BAT0', type='Battery', scope='System', present='1')
        ac = self.supply('AC', type='Mains', online='1')
        self.assertEqual(self.monitor.power, 'ac')
        (ac / 'online').write_text('0\n')
        self.assertEqual(self.monitor.power, 'battery')
        (ac / 'online').write_text('1\n')
        self.assertEqual(self.monitor.power, 'ac')

    def test_battery_only_and_absent_battery(self):
        battery = self.supply('BAT0', type='Battery')
        self.assertEqual(self.monitor.power, 'battery')
        (battery / 'present').write_text('0')
        self.assertEqual(self.monitor.power, 'unknown')

    def test_mains_only_and_multiple_supplies(self):
        self.supply('AC', type='Mains', online='0')
        self.assertEqual(self.monitor.power, 'unknown')
        usb = self.supply('ucsi-source', type='USB_PD', online='1', scope='System')
        self.assertEqual(self.monitor.power, 'ac')
        (usb / 'online').write_text('0')
        self.supply('BAT0', type='Battery')
        self.assertEqual(self.monitor.power, 'battery')

    def test_malformed_external_supply_fails_closed_even_with_online_source(self):
        self.supply('AC', type='Mains', online='1')
        other = self.supply('other', type='USB_C')
        for value in ('', '2', '-1', 'true', '1\n0'):
            (other / 'online').write_text(value)
            self.assertEqual(self.monitor.power, 'unknown')
        (other / 'online').unlink()
        self.assertEqual(self.monitor.power, 'unknown')

    def test_invalid_scope_type_and_battery_presence_fail_closed(self):
        supply = self.supply('supply', scope='invalid', type='Battery')
        self.assertEqual(self.monitor.power, 'unknown')
        (supply / 'scope').write_text('System')
        (supply / 'type').write_text('invalid')
        self.assertEqual(self.monitor.power, 'unknown')
        (supply / 'type').write_text('Battery')
        (supply / 'present').write_text('invalid')
        self.assertEqual(self.monitor.power, 'unknown')

    def test_supply_read_errors_fail_closed(self):
        self.supply('AC', type='Mains', online='1')
        for error in (PermissionError('denied'), FileNotFoundError('removed'), UnicodeError('invalid')):
            with patch.object(Path, 'read_text', side_effect=error):
                self.assertEqual(self.monitor.power, 'unknown')
        with patch.object(Path, 'iterdir', side_effect=OSError('missing root')):
            self.assertEqual(self.monitor.power, 'unknown')

    def test_registration_is_lazy_deduplicated_and_testing_avoids_bus(self):
        self.glib.timeout_add_seconds.assert_not_called()
        self.supply('BAT0', type='Battery')
        controller = Mock()
        with patch('openrazer_daemon.misc.fan_power.dbus.SystemBus') as bus:
            self.monitor.register(controller)
            self.monitor.register(controller)
        bus.assert_not_called()
        self.glib.timeout_add_seconds.assert_called_once_with(1, self.monitor._poll)
        controller.update_power.assert_called_once_with('battery')
        controller.check_status.assert_not_called()

    def test_poll_notifies_all_controllers_despite_failure(self):
        first, second = Mock(), Mock()
        first.update_power.side_effect = RuntimeError('failed')
        first.check_status.side_effect = RuntimeError('failed')
        self.monitor.register(first)
        self.monitor.register(second)
        second.reset_mock()
        self.supply('AC', type='Mains', online='1')
        self.assertTrue(self.monitor._poll())
        second.update_power.assert_called_once_with('ac')
        second.check_status.assert_called_once_with()

    def test_sleep_registration_and_wake_callbacks(self):
        first, second = Mock(), Mock()
        self.monitor.register(first)
        first.prepare_for_sleep.side_effect = RuntimeError('restore failed')
        self.monitor.prepare_for_sleep(True)
        self.monitor.register(second)
        second.prepare_for_sleep.assert_called_once_with(True)
        self.monitor.prepare_for_sleep(False)
        self.assertEqual(second.prepare_for_sleep.call_args_list[-1].args, (False,))

    def test_unregister_and_close_remove_timer(self):
        first, second = Mock(), Mock()
        self.monitor.register(first)
        self.monitor.register(second)
        self.monitor.unregister(first)
        self.glib.source_remove.assert_not_called()
        self.monitor.unregister(second)
        self.glib.source_remove.assert_called_once_with(42)
        self.monitor.register(first)
        self.monitor.close()
        self.monitor.close()
        self.assertFalse(self.monitor._poll())
        self.assertEqual(self.glib.source_remove.call_count, 2)
        with self.assertRaises(RuntimeError):
            self.monitor.register(second)

    def test_logind_filter_inhibitor_and_release_after_callbacks(self):
        bus, manager, proxy, signal = Mock(), Mock(), Mock(), Mock()
        bus.get_object.return_value = proxy
        proxy.Get.return_value = False
        bus.add_signal_receiver.return_value = signal
        descriptors = []

        def inhibit(*_args):
            descriptor = os.open(os.devnull, os.O_RDONLY)
            descriptors.append(descriptor)
            return Mock(take=Mock(return_value=descriptor))

        manager.Inhibit.side_effect = inhibit
        self.monitor._testing = False
        first, second = Mock(), Mock()
        with patch('openrazer_daemon.misc.fan_power.dbus.SystemBus', return_value=bus), patch('openrazer_daemon.misc.fan_power.dbus.Interface', return_value=manager) as interface:
            self.monitor.register(first)
            self.monitor.register(second)
        interface.assert_called_once_with(proxy, 'org.freedesktop.login1.Manager')
        bus.get_object.assert_called_once_with('org.freedesktop.login1', '/org/freedesktop/login1')
        proxy.Get.assert_called_once_with('org.freedesktop.login1.Manager', 'PreparingForSleep', dbus_interface='org.freedesktop.DBus.Properties')
        bus.add_signal_receiver.assert_called_once_with(self.monitor.prepare_for_sleep, signal_name='PrepareForSleep', dbus_interface='org.freedesktop.login1.Manager', bus_name='org.freedesktop.login1', path='/org/freedesktop/login1')
        manager.Inhibit.assert_called_once_with('sleep', 'OpenRazer', 'Restore automatic fan control', 'delay')
        self.assertTrue(self.monitor.sleep_supported)

        def before_sleep(sleeping):
            if sleeping:
                os.fstat(descriptors[-1])

        first.prepare_for_sleep.side_effect = before_sleep
        second.prepare_for_sleep.side_effect = RuntimeError('restore failed')
        self.monitor.prepare_for_sleep(True)
        with self.assertRaises(OSError):
            os.fstat(descriptors[-1])
        self.monitor.prepare_for_sleep(False)
        self.assertEqual(len(descriptors), 2)
        os.fstat(descriptors[-1])
        self.monitor.close()
        signal.remove.assert_called_once_with()
        with self.assertRaises(OSError):
            os.fstat(descriptors[-1])

    def test_unavailable_logind_does_not_prevent_power_monitoring(self):
        self.monitor._testing = False
        controller = Mock()
        with patch('openrazer_daemon.misc.fan_power.dbus.SystemBus', side_effect=RuntimeError('unavailable')):
            self.monitor.register(controller)
        self.assertFalse(self.monitor.sleep_supported)
        self.assertTrue(self.monitor._poll())
        controller.check_status.assert_called_once_with()

    def test_inhibitor_failure_retains_signal_and_retries_after_wake(self):
        bus, manager = Mock(), Mock()
        bus.get_object.return_value.Get.return_value = False
        manager.Inhibit.side_effect = RuntimeError('denied')
        self.monitor._testing = False
        with patch('openrazer_daemon.misc.fan_power.dbus.SystemBus', return_value=bus), patch('openrazer_daemon.misc.fan_power.dbus.Interface', return_value=manager):
            self.monitor.register(Mock())
        self.assertFalse(self.monitor.sleep_supported)
        self.monitor.prepare_for_sleep(True)
        self.monitor.prepare_for_sleep(False)
        self.assertEqual(manager.Inhibit.call_count, 2)
        self.monitor.close()
        bus.add_signal_receiver.return_value.remove.assert_called_once_with()

    def test_first_registration_during_pending_sleep_does_not_acquire_inhibitor(self):
        bus, manager = Mock(), Mock()
        bus.get_object.return_value.Get.return_value = True
        self.monitor._testing = False
        controller = Mock()
        with patch('openrazer_daemon.misc.fan_power.dbus.SystemBus', return_value=bus), patch('openrazer_daemon.misc.fan_power.dbus.Interface', return_value=manager):
            self.monitor.register(controller)
        controller.prepare_for_sleep.assert_called_once_with(True)
        manager.Inhibit.assert_not_called()
        self.assertFalse(self.monitor.sleep_supported)

    def test_uncertain_pending_sleep_state_blocks_restoration_until_wake(self):
        bus, manager = Mock(), Mock()
        bus.get_object.return_value.Get.side_effect = RuntimeError('unavailable property')
        self.monitor._testing = False
        controller = Mock()
        with patch('openrazer_daemon.misc.fan_power.dbus.SystemBus', return_value=bus), patch('openrazer_daemon.misc.fan_power.dbus.Interface', return_value=manager):
            self.monitor.register(controller)
        controller.prepare_for_sleep.assert_called_once_with(True)
        manager.Inhibit.assert_not_called()
        self.assertFalse(self.monitor.sleep_supported)
        manager.Inhibit.side_effect = RuntimeError('denied')
        self.monitor.prepare_for_sleep(False)
        self.assertEqual(controller.prepare_for_sleep.call_args_list[-1].args, (False,))
        manager.Inhibit.assert_called_once_with('sleep', 'OpenRazer', 'Restore automatic fan control', 'delay')

    def test_sleep_signal_during_snapshot_is_not_overwritten(self):
        bus, manager = Mock(), Mock()

        def snapshot(*_args, **_kwargs):
            self.monitor.prepare_for_sleep(True)
            return False

        bus.get_object.return_value.Get.side_effect = snapshot
        self.monitor._testing = False
        controller = Mock()
        with patch('openrazer_daemon.misc.fan_power.dbus.SystemBus', return_value=bus), patch('openrazer_daemon.misc.fan_power.dbus.Interface', return_value=manager):
            self.monitor.register(controller)
        self.assertTrue(self.monitor._sleeping)
        manager.Inhibit.assert_not_called()
        self.assertEqual(controller.prepare_for_sleep.call_args_list[-1].args, (True,))

    def test_closed_monitor_cannot_authorize_manual_power(self):
        self.supply('AC', type='Mains', online='1')
        self.assertEqual(self.monitor.power, 'ac')
        self.monitor.close()
        with patch.object(Path, 'iterdir', side_effect=AssertionError('Unexpected supply access')):
            self.assertEqual(self.monitor.power, 'unknown')

    def test_close_during_supply_read_does_not_return_stale_ac(self):
        self.supply('AC', type='Mains', online='1')
        read = self.monitor._read

        def read_and_close(path, *args):
            value = read(path, *args)
            if path.name == 'online':
                self.monitor.close()
            return value

        with patch.object(self.monitor, '_read', side_effect=read_and_close):
            self.assertEqual(self.monitor.power, 'unknown')

    def test_close_during_timer_creation_aborts_registration(self):
        def timer(*_args):
            self.monitor.close()
            return 42

        self.glib.timeout_add_seconds.side_effect = timer
        controller = Mock()
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            self.monitor.register(controller)
        self.glib.source_remove.assert_called_once_with(42)
        controller.update_power.assert_not_called()

    def test_close_during_logind_snapshot_aborts_registration(self):
        bus, manager = Mock(), Mock()

        def snapshot(*_args, **_kwargs):
            self.monitor.close()
            return False

        bus.get_object.return_value.Get.side_effect = snapshot
        self.monitor._testing = False
        controller = Mock()
        with patch('openrazer_daemon.misc.fan_power.dbus.SystemBus', return_value=bus), patch('openrazer_daemon.misc.fan_power.dbus.Interface', return_value=manager):
            with self.assertRaisesRegex(RuntimeError, 'closed'):
                self.monitor.register(controller)
        self.assertEqual(self.monitor.power, 'unknown')
        self.assertIsNone(self.monitor._source)
        self.assertIsNone(self.monitor._signal)
        bus.add_signal_receiver.return_value.remove.assert_called_once_with()
        manager.Inhibit.assert_not_called()
        controller.update_power.assert_not_called()

    def test_close_during_inhibit_closes_late_descriptor(self):
        bus, manager = Mock(), Mock()
        bus.get_object.return_value.Get.return_value = False
        descriptors = []

        def inhibit(*_args):
            self.monitor.close()
            descriptor = os.open(os.devnull, os.O_RDONLY)
            descriptors.append(descriptor)
            return Mock(take=Mock(return_value=descriptor))

        manager.Inhibit.side_effect = inhibit
        self.monitor._testing = False
        controller = Mock()
        with patch('openrazer_daemon.misc.fan_power.dbus.SystemBus', return_value=bus), patch('openrazer_daemon.misc.fan_power.dbus.Interface', return_value=manager):
            with self.assertRaisesRegex(RuntimeError, 'closed'):
                self.monitor.register(controller)
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])
        self.assertFalse(self.monitor.sleep_supported)
        self.assertIsNone(self.monitor._inhibitor)
        bus.add_signal_receiver.return_value.remove.assert_called_once_with()
        controller.update_power.assert_not_called()


if __name__ == '__main__':
    unittest.main()
