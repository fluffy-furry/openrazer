#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later

import configparser
import errno
import logging
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'daemon'))

from openrazer_daemon.misc.fan_control import FanControl


class BladeControlTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.state('auto', 0)
        for name, value in {'fan_modes': '81 1', 'fan_rpm_monitor': '6', 'fan_limits': '2300 2900 4300', 'fan_rpm': '1 2900\n2 2900\n',
                            'fan_groups': 'cpu_gpu 1,2\nbattery 3,4\n', 'fan_control_select': '',
                            'fan_target_ids': '1,2\n', 'fan_control_targets': ''}.items():
            (self.path / name).write_text(value)
        config = configparser.ConfigParser()
        config.read_dict({'Startup': {'restore_persistence': 'false'}})
        self.device = SimpleNamespace(config=config, persistence=configparser.ConfigParser(), storage_name='FANCONTROL',
                                      logger=logging.getLogger('fan-control-test'), get_driver_path=lambda name: str(self.path / name), disable_persistence=False,
                                      METHODS=['set_fan_manual_fans', 'set_fan_manual_targets'])
        self.monitor = Mock()
        self.monitor.status = ('idle', 0, {}, '')
        self.monitor.start.side_effect = lambda rpm, *_: setattr(self.monitor, 'status', ('settling', rpm, {}, ''))

        def start_targets(targets, monitored, _modes):
            target = next(iter(targets.values())) if len(set(targets.values())) == 1 else 0
            self.monitor.status = ('settling' if monitored else 'accepted', target, {}, '')

        self.monitor.start_targets.side_effect = start_targets
        self.power = SimpleNamespace(power='ac', register=lambda control: control.update_power(self.power.power), unregister=Mock())
        with patch('openrazer_daemon.misc.fan_control.FanMonitor', return_value=self.monitor):
            self.control = FanControl(self.device)
        self.writes = []
        self.write_error = None
        self.after_write = None
        self.control._write = self.write
        self.control._write_selected = self.write_selected
        self.control._write_targets = self.write_targets
        self.selected_writes = []
        self.target_writes = []
        self.addCleanup(self.control.close)
        self.control.attach_power(self.power)

    def state(self, mode, rpm, performance=0, ids=(1, 2, 3, 4)):
        (self.path / 'fan_state').write_text(''.join(f'{fan_id} {performance} {mode} {rpm}\n' for fan_id in ids))

    def write(self, value):
        self.writes.append(value)
        if self.write_error is not None:
            raise self.write_error
        if value == 'auto':
            self.state('auto', 0)
        else:
            self.state('manual', int(value))
        if self.after_write:
            self.after_write(value)

    def write_selected(self, value):
        self.selected_writes.append(value)
        if self.write_error is not None:
            raise self.write_error
        rows = {}
        for line in (self.path / 'fan_state').read_text().splitlines():
            fan_id, performance, mode, rpm = line.split()
            rows[int(fan_id)] = (int(performance), mode, int(rpm))
        command, selected = value.split(' ', 1)
        if command == 'auto':
            for text in selected.split(','):
                fan_id = int(text)
                rows[fan_id] = (rows[fan_id][0], 'auto', 0)
        else:
            for entry in selected.split(','):
                fan_id_text, rpm_text = entry.split(':')
                fan_id = int(fan_id_text)
                rows[fan_id] = (rows[fan_id][0], 'manual', int(rpm_text))
        (self.path / 'fan_state').write_text(''.join(f'{fan_id} {performance} {mode} {rpm}\n' for fan_id, (performance, mode, rpm) in sorted(rows.items())))
        if self.after_write:
            self.after_write(value)

    def write_targets(self, targets):
        self.target_writes.append(dict(targets))
        if self.write_error is not None:
            raise self.write_error
        rows = {}
        for line in (self.path / 'fan_state').read_text().splitlines():
            fan_id, performance, mode, rpm = line.split()
            rows[int(fan_id)] = (int(performance), mode, int(rpm))
        for fan_id, rpm in targets.items():
            rows[fan_id] = (rows[fan_id][0], 'manual', rpm)
        (self.path / 'fan_state').write_text(''.join(f'{fan_id} {performance} {mode} {rpm}\n' for fan_id, (performance, mode, rpm) in sorted(rows.items())))

    def test_manual_targets_start_global_manual_then_change_target_only(self):
        self.control.set_manual_targets({1: 4300})
        self.assertEqual(self.writes, ['2900'])
        self.assertEqual(self.target_writes, [{1: 4300}])
        self.assertEqual(self.control._target_owned, {1: 4300, 2: 2900, 3: 2900, 4: 2900})
        self.assertEqual(self.control._target_targets, {1: 4300, 2: 2900})
        self.assertEqual(self.control._target_base_rpm, 2900)
        self.assertEqual(self.device.persistence.get('FANCONTROL', 'fan_base_rpm'), '2900')
        self.assertEqual(self.control._ownership(), 'owned')
        self.assertEqual(self.device.persistence.get('FANCONTROL', 'fan_mode'), 'targets')
        self.monitor.start_targets.assert_called_with({1: 4300, 2: 2900}, (1, 2), {1: 0, 2: 0})
        self.control.set_manual_targets({2: 3000})
        self.assertEqual(self.writes, ['2900'])
        self.assertEqual(self.target_writes, [{1: 4300}, {2: 3000}])
        self.assertEqual(self.control._target_targets, {1: 4300, 2: 3000})
        self.assertEqual(self.control._target_owned, {1: 4300, 2: 3000, 3: 2900, 4: 2900})
        self.assertEqual(self.control._ownership(), 'owned')
        self.control.update_power('battery')
        self.control.update_power('ac')
        self.assertEqual(self.target_writes[-1], {1: 4300, 2: 3000})
        self.assertEqual(self.control._target_owned, {1: 4300, 2: 3000, 3: 2900, 4: 2900})

    def test_manual_targets_preserve_scalar_baseline_and_restore_it(self):
        self.control.set_manual(4300)
        self.control.set_manual_targets({1: 4200})
        self.assertEqual(self.writes, ['4300'])
        self.assertEqual(self.control._target_owned, {1: 4200, 2: 4300, 3: 4300, 4: 4300})
        self.assertEqual(self.control._target_targets, {1: 4200, 2: 4300})
        self.assertEqual(self.device.persistence.get('FANCONTROL', 'fan_targets'), '1:4200,2:4300')
        self.assertEqual(self.device.persistence.get('FANCONTROL', 'fan_base_rpm'), '4300')
        self.control.update_power('battery')
        self.control.update_power('ac')
        self.assertEqual(self.writes, ['4300', 'auto', '4300'])
        self.assertEqual(self.control._target_owned, {1: 4200, 2: 4300, 3: 4300, 4: 4300})
        self.control.prepare_for_sleep(True)
        self.control.prepare_for_sleep(False)
        self.assertEqual(self.writes[-1], '4300')
        self.assertEqual(self.control._target_owned, {1: 4200, 2: 4300, 3: 4300, 4: 4300})

    def test_invalid_target_preserves_existing_scalar_manual_setting(self):
        self.control.set_manual(4300)
        for targets in ({3: 2900}, {1: 5000}):
            with self.subTest(targets=targets), self.assertRaises(ValueError):
                self.control.set_manual_targets(targets)
        self.assertEqual(self.writes, ['4300'])
        self.assertEqual(self.control._ownership(), 'owned')
        self.assertEqual(self.device.persistence.get('FANCONTROL', 'fan_mode'), 'manual')

    def test_missing_targetable_id_rejects_before_any_write(self):
        self.state('auto', 0, ids=(1, 3, 4))
        with self.assertRaises(RuntimeError):
            self.control.set_manual_targets({1: 4300})
        self.assertEqual(self.writes, [])
        self.assertEqual(self.target_writes, [])

    def test_saved_target_baseline_restores_all_fan_state(self):
        self.device.config.set('Startup', 'restore_persistence', 'true')
        self.device.persistence.read_dict({'FANCONTROL': {'fan_mode': 'targets', 'fan_targets': '1:4200,2:4300',
                                                          'fan_base_rpm': '4300', 'fan_performance_mode': '0'}})
        self.control.restore_preferences()
        self.assertEqual(self.writes, ['4300'])
        self.assertEqual(self.target_writes, [{1: 4200, 2: 4300}])
        self.assertEqual(self.control._target_owned, {1: 4200, 2: 4300, 3: 4300, 4: 4300})

    def test_invalid_saved_target_baseline_is_ignored(self):
        self.device.config.set('Startup', 'restore_persistence', 'true')
        self.device.persistence.read_dict({'FANCONTROL': {'fan_mode': 'targets', 'fan_targets': '1:4200,2:4300',
                                                          'fan_base_rpm': '2950', 'fan_performance_mode': '0'}})
        self.control.restore_preferences()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.target_writes, [])

    def test_saved_targets_do_not_restore_without_experimental_attributes(self):
        self.device.config.set('Startup', 'restore_persistence', 'true')
        self.device.persistence.read_dict({'FANCONTROL': {'fan_mode': 'targets', 'fan_targets': '1:4300,2:2900',
                                                          'fan_base_rpm': '2900', 'fan_performance_mode': '0'}})
        for missing in ('fan_target_ids', 'fan_control_targets'):
            with self.subTest(missing=missing):
                path = self.path / missing
                value = path.read_text()
                path.unlink()
                self.control.restore_preferences()
                self.control.update_power('battery')
                self.control.update_power('ac')
                self.control.prepare_for_sleep(True)
                self.control.prepare_for_sleep(False)
                self.assertEqual(self.writes, [])
                self.assertEqual(self.target_writes, [])
                self.assertEqual(self.control._target_owned, {})
                self.assertEqual(self.control._target_targets, {})
                path.write_text(value)

    def test_manual_targets_reject_unregistered_id_and_external_manual(self):
        with self.assertRaises(ValueError):
            self.control.set_manual_targets({3: 2900})
        self.state('manual', 2900)
        with self.assertRaises(RuntimeError):
            self.control.set_manual_targets({1: 4300})
        self.assertEqual(self.writes, [])
        self.assertEqual(self.target_writes, [])

    def test_manual_target_write_failure_restores_all_auto(self):
        with patch.object(self.control, '_write_targets', side_effect=OSError(errno.EIO, 'target failed')):
            with self.assertRaises(OSError):
                self.control.set_manual_targets({1: 4300})
        self.assertEqual(self.writes, ['2900', 'auto'])
        self.assertEqual(self.control._target_owned, {})
        self.assertEqual((self.path / 'fan_state').read_text(), '1 0 auto 0\n2 0 auto 0\n3 0 auto 0\n4 0 auto 0\n')

    def test_manual_targets_restore_all_auto_on_power_loss_and_close(self):
        self.control.set_manual_targets({1: 4300})
        self.control.update_power('battery')
        self.assertEqual(self.writes, ['2900', 'auto'])
        self.assertEqual(self.control._target_owned, {})
        self.assertEqual(self.control._target_targets, {1: 4300, 2: 2900})
        self.control.update_power('ac')
        self.assertEqual(self.writes, ['2900', 'auto', '2900'])
        self.assertEqual(self.target_writes, [{1: 4300}, {1: 4300, 2: 2900}])
        self.control.close()
        self.assertEqual(self.writes[-1], 'auto')

    def test_manual_targets_restore_all_auto_on_sleep_and_external_change(self):
        self.control.set_manual_targets({1: 4300})
        self.control.prepare_for_sleep(True)
        self.assertEqual(self.writes[-1], 'auto')
        self.control.prepare_for_sleep(False)
        self.assertEqual(self.writes[-1], '2900')
        self.assertEqual(self.control._target_owned[1], 4300)
        self.state('manual', 2900)
        self.monitor.status = ('cancelled', 0, {}, 'Fan mode or target changed')
        self.control.check_status()
        self.assertEqual(self.writes[-1], '2900')
        self.assertEqual((self.path / 'fan_state').read_text(), '1 0 manual 2900\n2 0 manual 2900\n3 0 manual 2900\n4 0 manual 2900\n')
        self.assertEqual(self.device.persistence.get('FANCONTROL', 'fan_mode'), 'auto')

    def test_new_target_request_relinquishes_external_takeover(self):
        self.control.set_manual_targets({1: 4300})
        self.state('manual', 2900)
        with self.assertRaises(RuntimeError):
            self.control.set_manual_targets({2: 3000})
        self.assertEqual(self.writes, ['2900'])
        self.assertEqual(self.target_writes, [{1: 4300}])
        self.assertFalse(self.control._owned)
        self.assertEqual(self.control._target_owned, {})

    def test_driver_setter_issues_one_write_syscall(self):
        with patch('openrazer_daemon.misc.fan_control.os.write', wraps=os.write) as write:
            self.control._write_file('fan_control_targets', '1:4300')
        write.assert_called_once()
        self.assertEqual((self.path / 'fan_control_targets').read_text(), '1:4300')
        with patch('openrazer_daemon.misc.fan_control.os.write', side_effect=OSError(errno.EIO, 'rejected')) as write:
            with self.assertRaises(OSError):
                self.control._write_file('fan_control_targets', '1:4300')
        write.assert_called_once()

    def test_idle_registration_and_close_do_not_access_fans(self):
        with patch('builtins.open', side_effect=AssertionError('Unexpected fan access')):
            self.assertEqual(self.control.status, ('idle', 0, {}, ''))
            self.control.update_power('battery')
            self.control.prepare_for_sleep(True)
            self.control.prepare_for_sleep(False)
            self.control.close()
        self.assertEqual(self.writes, [])

    def test_manual_starts_monitor_for_registered_sensors_and_all_mode_snapshots(self):
        self.control.set_manual(2900)
        self.assertEqual(self.writes, ['2900'])
        self.monitor.start.assert_called_once_with(2900, (1, 2), {1: 0, 2: 0, 3: 0, 4: 0})
        self.assertEqual(self.control.status[0:2], ('settling', 2900))
        self.assertEqual(self.device.persistence.get('FANCONTROL', 'fan_performance_mode'), '0')

    def test_invalid_requests_do_not_touch_existing_manual_setting(self):
        self.control.set_manual(2900)
        for value in (True, 0, -100, 2350, 25600, '2900', 2900.0, 2200, 4400):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.control.set_manual(value)
        self.assertEqual(self.writes, ['2900'])

    def test_manual_requires_ac_without_hardware_access(self):
        for power in ('battery', 'unknown'):
            self.power.power = power
            with self.subTest(power=power), patch('builtins.open', side_effect=AssertionError('Unexpected fan read')):
                with self.assertRaises(RuntimeError):
                    self.control.set_manual(2900)
        self.assertEqual(self.writes, [])

    def test_manual_requires_current_supported_mode_and_telemetry(self):
        self.state('auto', 0, 6)
        with self.assertRaises(RuntimeError):
            self.control.set_manual(2900)
        self.state('auto', 0, ids=(1, 3, 4))
        with self.assertRaises(RuntimeError):
            self.control.set_manual(2900)
        self.assertEqual(self.writes, [])

    def test_mode_policy_is_read_from_driver(self):
        (self.path / 'fan_modes').write_text('85 5')
        self.state('auto', 0, 2)
        self.control.set_manual(2900)
        self.assertEqual(self.writes, ['2900'])
        self.monitor.start.assert_called_once_with(2900, (1, 2), {1: 2, 2: 2, 3: 2, 4: 2})

    def test_auto_does_not_depend_on_power_limits_or_state(self):
        self.power.power = 'unknown'
        for path in self.path.iterdir():
            path.unlink()
        self.control.set_auto()
        self.assertEqual(self.writes, ['auto'])
        self.assertEqual(self.control.status, ('auto', 0, {}, ''))

    def test_power_loss_and_return_restore_requested_manual_once(self):
        self.control.set_manual(2900)
        self.power.power = 'battery'
        self.control.update_power('battery')
        self.control.update_power('battery')
        self.assertEqual(self.control.status[0:2], ('suspended', 2900))
        self.power.power = 'ac'
        self.control.update_power('ac')
        self.control.update_power('ac')
        self.assertEqual(self.writes, ['2900', 'auto', '2900'])

    def test_unknown_power_also_restores_auto(self):
        self.control.set_manual(2900)
        self.power.power = 'unknown'
        self.control.update_power('unknown')
        self.assertEqual(self.writes, ['2900', 'auto'])
        self.assertIn('unknown', self.control.status[3])

    def test_resume_rechecks_mode_and_limits_without_changing_performance(self):
        self.control.set_manual(2900)
        self.control.prepare_for_sleep(True)
        self.state('auto', 0, 6)
        self.control.prepare_for_sleep(False)
        self.assertEqual(self.writes, ['2900', 'auto'])
        self.assertEqual(self.control.status[0], 'suspended')
        self.control.prepare_for_sleep(True)
        self.state('auto', 0)
        (self.path / 'fan_limits').write_text('3000 3500 4000')
        self.control.prepare_for_sleep(False)
        self.assertEqual(self.writes, ['2900', 'auto'])

    def test_sleep_restore_is_separate_from_duplicate_wake_notifications(self):
        self.control.set_manual(2900)
        self.control.prepare_for_sleep(False)
        self.control.prepare_for_sleep(True)
        self.control.prepare_for_sleep(True)
        with self.assertRaises(RuntimeError):
            self.control.set_manual(2900)
        self.control.prepare_for_sleep(False)
        self.control.prepare_for_sleep(False)
        self.assertEqual(self.writes, ['2900', 'auto', '2900'])

    def test_explicit_auto_cancels_saved_manual_request(self):
        self.control.set_manual(2900)
        self.control.set_auto()
        self.control.update_power('battery')
        self.control.update_power('ac')
        self.control.prepare_for_sleep(True)
        self.control.prepare_for_sleep(False)
        self.control.close()
        self.assertEqual(self.writes, ['2900', 'auto'])
        self.assertFalse(self.device.persistence.has_option('FANCONTROL', 'fan_rpm'))

    def test_shutdown_restores_owned_control_and_releases_monitor(self):
        self.control.set_manual(2900)
        self.control.close()
        self.control.close()
        self.assertEqual(self.writes, ['2900', 'auto'])
        self.monitor.close.assert_called_once()
        self.power.unregister.assert_called_once_with(self.control)

    def test_power_change_during_manual_write_restores_auto_and_reports_error(self):
        self.after_write = lambda _: setattr(self.power, 'power', 'battery')
        with self.assertRaisesRegex(RuntimeError, 'AC power changed'):
            self.control.set_manual(2900)
        self.assertEqual(self.writes, ['2900', 'auto'])
        self.monitor.start.assert_not_called()

    def test_manual_transport_failure_is_not_replayed(self):
        self.write_error = OSError(errno.EIO, 'transport failed')
        with self.assertRaises(OSError):
            self.control.set_manual(2900)
        self.assertEqual(self.writes, ['2900', 'auto'])
        for _ in range(10):
            self.control.check_status()
        self.assertEqual(self.writes, ['2900', 'auto', 'auto', 'auto'])
        self.assertEqual(self.control.status[0], 'error')
        self.assertIn('automatic restoration failed', self.control.status[3])

    def test_transient_automatic_failure_has_bounded_automatic_only_recovery(self):
        self.control.set_manual(2900)
        self.write_error = OSError(errno.EBUSY, 'busy')
        self.power.power = 'battery'
        self.control.update_power('battery')
        self.assertEqual(self.control.status[0], 'error')
        self.write_error = None
        self.control.check_status()
        self.control.check_status()
        self.assertEqual(self.writes, ['2900', 'auto', 'auto'])
        self.assertEqual(self.control.status[0], 'suspended')

    def test_monitor_error_restores_auto_but_timeout_reports_unconfirmed_speed(self):
        self.control.set_manual(2900)
        self.monitor.status = ('timeout', 2900, {1: 2800, 2: 2900}, 'deadline')
        self.control.check_status()
        self.assertEqual(self.control.status[0], 'timeout')
        self.assertEqual(self.writes, ['2900'])
        self.monitor.status = ('error', 2900, {}, 'read failed')
        self.control.check_status()
        self.assertEqual(self.writes, ['2900', 'auto'])
        self.assertEqual(self.control.status[0], 'error')

    def test_external_change_relinquishes_manual_ownership(self):
        self.control.set_manual(2900)
        self.monitor.status = ('cancelled', 2900, {}, 'Fan mode or target changed')
        self.control.check_status()
        self.control.close()
        self.assertEqual(self.writes, ['2900'])
        self.assertEqual(self.device.persistence.get('FANCONTROL', 'fan_mode'), 'auto')

    def test_disabled_persistence_is_respected(self):
        self.device.disable_persistence = True
        self.control.set_manual(2900)
        self.assertEqual(self.device.persistence.sections(), [])

    def test_startup_restoration_requires_opt_in_and_valid_saved_policy(self):
        self.device.persistence.read_dict({'FANCONTROL': {'fan_mode': 'manual', 'fan_rpm': '2900', 'fan_performance_mode': '0'}})
        self.control.restore_preferences()
        self.assertEqual(self.writes, [])
        self.device.config.set('Startup', 'restore_persistence', 'true')
        self.control.restore_preferences()
        self.assertEqual(self.writes, ['2900'])

    def test_missing_or_invalid_saved_preferences_never_write(self):
        self.device.config.set('Startup', 'restore_persistence', 'true')
        for values in ({'fan_mode': 'manual'}, {'fan_mode': 'manual', 'fan_rpm': 'bad'},
                       {'fan_mode': 'manual', 'fan_rpm': '2900'}, {'fan_mode': 'manual', 'fan_rpm': '2950', 'fan_performance_mode': '0'}):
            self.device.persistence.remove_section('FANCONTROL')
            self.device.persistence.read_dict({'FANCONTROL': values})
            self.control.restore_preferences()
        self.assertEqual(self.writes, [])

    def test_saved_other_performance_is_not_silently_applied(self):
        self.device.config.set('Startup', 'restore_persistence', 'true')
        self.device.persistence.read_dict({'FANCONTROL': {'fan_mode': 'manual', 'fan_rpm': '2900', 'fan_performance_mode': '4'}})
        self.control.restore_preferences()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.control.status[0], 'suspended')

    def test_cleanup_does_not_overwrite_an_external_manual_setting(self):
        self.control.set_manual(2900)
        self.state('manual', 3300)
        self.control.close()
        self.assertEqual(self.writes, ['2900'])
        self.assertEqual(self.control.status[0], 'cancelled')

    def test_resume_does_not_overwrite_external_manual_control(self):
        self.control.set_manual(2900)
        self.control.prepare_for_sleep(True)
        self.state('manual', 3300)
        self.control.prepare_for_sleep(False)
        self.assertEqual(self.writes, ['2900', 'auto'])
        self.assertIn('Another manual', self.control.status[3])
        self.assertEqual(self.control.status[0], 'cancelled')
        self.assertEqual(self.device.persistence.get('FANCONTROL', 'fan_mode'), 'auto')
        self.state('auto', 0)
        self.power.power = 'battery'
        self.control.update_power('battery')
        self.power.power = 'ac'
        self.control.update_power('ac')
        self.assertEqual(self.writes, ['2900', 'auto'])

    def test_failed_ownership_read_still_attempts_automatic_recovery(self):
        self.control.set_manual(2900)
        (self.path / 'fan_state').unlink()
        self.control.close()
        self.assertEqual(self.writes, ['2900', 'auto'])

    def test_firmware_automatic_transition_preserves_ac_preference(self):
        self.control.set_manual(2900)
        self.state('auto', 0, 6)
        self.power.power = 'battery'
        self.control.update_power('battery')
        self.assertEqual(self.writes, ['2900'])
        self.state('auto', 0)
        self.power.power = 'ac'
        self.control.update_power('ac')
        self.assertEqual(self.writes, ['2900', '2900'])

    def test_ownership_is_rechecked_after_settling_completes(self):
        self.control.set_manual(2900)
        self.monitor.status = ('reached', 2900, {1: 2900, 2: 2900}, '')
        self.state('manual', 3300)
        with patch('openrazer_daemon.misc.fan_control.time.monotonic', return_value=self.control._last_ownership_check + 6):
            self.control.check_status()
        self.assertEqual(self.control.status[0], 'cancelled')
        self.control.close()
        self.assertEqual(self.writes, ['2900'])

    def test_failed_resume_and_recovery_remain_an_error(self):
        self.control.set_manual(2900)
        self.control.prepare_for_sleep(True)
        self.write_error = OSError(errno.EIO, 'transport failed')
        self.control.prepare_for_sleep(False)
        self.assertEqual(self.writes, ['2900', 'auto', '2900', 'auto'])
        self.assertEqual(self.control.status[0], 'error')
        self.assertIn('automatic restoration failed', self.control.status[3])

    def test_selected_manual_replaces_only_prior_owned_ids(self):
        self.control.set_manual_fans({1: 2900, 3: 3000})
        self.assertEqual(self.selected_writes, ['manual 1:2900,3:3000'])
        self.monitor.start_targets.assert_called_once_with({1: 2900, 3: 3000}, (1,), {1: 0, 3: 0})
        self.assertEqual(self.device.persistence.get('FANCONTROL', 'fan_mode'), 'selective')
        self.assertEqual(self.device.persistence.get('FANCONTROL', 'fan_targets'), '1:2900,3:3000')
        self.assertEqual(self.control.status[1], 0)
        self.control.set_manual_fans({2: 3100})
        self.assertEqual(self.selected_writes, ['manual 1:2900,3:3000', 'auto 1,3', 'manual 2:3100'])
        self.assertEqual((self.path / 'fan_state').read_text(), '1 0 auto 0\n2 0 manual 3100\n3 0 auto 0\n4 0 auto 0\n')

    def test_selected_invalid_request_leaves_existing_request_untouched(self):
        self.control.set_manual_fans({1: 2900})
        for targets in ({}, {1: 2950}, {0: 2900}, {5: 2900}, {1: 4400}, {True: 2900}):
            with self.subTest(targets=targets), self.assertRaises(ValueError):
                self.control.set_manual_fans(targets)
        self.assertEqual(self.selected_writes, ['manual 1:2900'])

    def test_selected_mode_policy_ignores_unselected_performance(self):
        (self.path / 'fan_modes').write_text('85 5')
        (self.path / 'fan_state').write_text('1 0 auto 0\n2 2 auto 0\n3 2 auto 0\n4 2 auto 0\n')
        self.control.set_manual_fans({1: 2900})
        self.assertEqual(self.selected_writes, ['manual 1:2900'])
        with self.assertRaises(RuntimeError):
            self.control.set_manual_fans({1: 2900, 2: 2900})
        self.assertEqual(self.selected_writes, ['manual 1:2900'])

    def test_selected_auto_and_shutdown_touch_only_owned_ids(self):
        self.control.set_manual_fans({1: 2900, 3: 3000})
        self.control.set_auto_fans((3,))
        self.assertEqual(self.control.status[0], 'settling')
        self.monitor.start_targets.assert_called_with({1: 2900}, (1,), {1: 0})
        self.control.close()
        self.assertEqual(self.selected_writes, ['manual 1:2900,3:3000', 'auto 3', 'auto 1'])
        self.assertEqual(self.writes, [])
        self.assertEqual((self.path / 'fan_state').read_text(), '1 0 auto 0\n2 0 auto 0\n3 0 auto 0\n4 0 auto 0\n')

    def test_selected_power_and_sleep_restore_selected_targets(self):
        self.control.set_manual_fans({1: 2900, 3: 3000})
        self.power.power = 'battery'
        self.control.update_power('battery')
        self.power.power = 'ac'
        self.control.update_power('ac')
        self.control.prepare_for_sleep(True)
        self.control.prepare_for_sleep(False)
        self.assertEqual(self.selected_writes, ['manual 1:2900,3:3000', 'auto 1,3', 'manual 1:2900,3:3000',
                                                'auto 1,3', 'manual 1:2900,3:3000'])
        self.assertEqual(self.writes, [])

    def test_selected_unmonitored_fan_is_accepted_without_rpm_claim(self):
        self.control.set_manual_fans({3: 2900})
        self.assertEqual(self.control.status[0], 'accepted')
        self.monitor.start_targets.assert_called_once_with({3: 2900}, (), {3: 0})

    def test_selected_firmware_coupling_restores_all_and_discards_intent(self):
        def coupled_write(value):
            self.selected_writes.append(value)
            self.state('manual', 2900)

        self.control._write_selected = coupled_write
        with self.assertRaises(RuntimeError):
            self.control.set_manual_fans({3: 2900})
        self.assertEqual(self.selected_writes, ['manual 3:2900'])
        self.assertEqual(self.writes, ['auto'])
        self.assertEqual((self.path / 'fan_state').read_text(), '1 0 auto 0\n2 0 auto 0\n3 0 auto 0\n4 0 auto 0\n')
        self.assertEqual(self.control._selected_owned, {})
        self.assertEqual(self.control._selected_targets, {})
        self.assertEqual(self.device.persistence.get('FANCONTROL', 'fan_mode'), 'auto')
        self.assertEqual(self.control.status[0], 'error')
        self.monitor.start_targets.assert_not_called()

    def test_selected_firmware_coupling_retries_failed_global_restore(self):
        def coupled_write(value):
            self.selected_writes.append(value)
            self.state('manual', 2900)

        self.control._write_selected = coupled_write
        self.write_error = OSError(errno.EIO, 'transport failed')
        with self.assertRaises(RuntimeError):
            self.control.set_manual_fans({3: 2900})
        self.assertEqual(self.writes, ['auto'])
        self.assertTrue(self.control._owned)
        self.assertEqual(self.control._selected_targets, {})
        self.assertEqual(self.device.persistence.get('FANCONTROL', 'fan_mode'), 'auto')
        self.write_error = None
        self.control.check_status()
        self.assertEqual(self.writes, ['auto', 'auto'])
        self.assertEqual((self.path / 'fan_state').read_text(), '1 0 auto 0\n2 0 auto 0\n3 0 auto 0\n4 0 auto 0\n')
        self.assertFalse(self.control._owned)

    def test_selected_auto_firmware_coupling_restores_all_and_discards_intent(self):
        self.control.set_manual_fans({1: 2900, 3: 2900})

        def coupled_write(value):
            self.selected_writes.append(value)
            self.state('auto', 0)

        self.control._write_selected = coupled_write
        with self.assertRaises(RuntimeError):
            self.control.set_auto_fans((3,))
        self.assertEqual(self.selected_writes, ['manual 1:2900,3:2900', 'auto 3'])
        self.assertEqual(self.writes, ['auto'])
        self.assertEqual((self.path / 'fan_state').read_text(), '1 0 auto 0\n2 0 auto 0\n3 0 auto 0\n4 0 auto 0\n')
        self.assertEqual(self.control._selected_owned, {})
        self.assertEqual(self.control._selected_targets, {})
        self.assertEqual(self.device.persistence.get('FANCONTROL', 'fan_mode'), 'auto')
        self.assertEqual(self.control.status[0], 'error')

    def test_selected_external_change_is_not_reset(self):
        self.control.set_manual_fans({1: 2900, 3: 3000})
        (self.path / 'fan_state').write_text('1 0 manual 3400\n2 0 auto 0\n3 0 manual 3000\n4 0 auto 0\n')
        self.control.close()
        self.assertEqual(self.selected_writes, ['manual 1:2900,3:3000', 'auto 3'])

    def test_selected_restore_retry_never_touches_external_id(self):
        self.control.set_manual_fans({1: 2900, 3: 3000})
        (self.path / 'fan_state').write_text('1 0 manual 3400\n2 0 auto 0\n3 0 manual 3000\n4 0 auto 0\n')
        self.write_error = OSError(errno.EBUSY, 'busy')
        self.power.power = 'battery'
        self.control.update_power('battery')
        self.assertEqual(self.control._selected_owned, {3: 3000})
        self.write_error = None
        self.control.check_status()
        self.assertEqual(self.selected_writes, ['manual 1:2900,3:3000', 'auto 3', 'auto 3'])
        self.assertEqual(self.control._selected_owned, {})

    def test_failed_selected_auto_retries_only_requested_id(self):
        self.control.set_manual_fans({1: 2900, 3: 3000})
        self.write_error = OSError(errno.EBUSY, 'busy')
        with self.assertRaises(OSError):
            self.control.set_auto_fans((3,))
        self.write_error = None
        self.control.check_status()
        self.control.close()
        self.assertEqual(self.selected_writes, ['manual 1:2900,3:3000', 'auto 3', 'auto 3', 'auto 1'])

    def test_failed_selected_auto_does_not_restore_external_change(self):
        self.control.set_manual_fans({1: 2900, 3: 3000})
        self.write_error = OSError(errno.EBUSY, 'busy')
        with self.assertRaises(OSError):
            self.control.set_auto_fans((3,))
        self.write_error = None
        (self.path / 'fan_state').write_text('1 0 manual 3400\n2 0 auto 0\n3 0 manual 3000\n4 0 auto 0\n')
        self.power.power = 'battery'
        self.control.update_power('battery')
        self.assertEqual(self.selected_writes, ['manual 1:2900,3:3000', 'auto 3', 'auto 3'])

    def test_failed_unowned_selected_auto_accepts_already_auto(self):
        self.write_error = OSError(errno.EBUSY, 'busy')
        with self.assertRaises(OSError):
            self.control.set_auto_fans((3,))
        self.write_error = None
        self.control.check_status()
        self.assertEqual(self.selected_writes, ['auto 3'])
        self.assertEqual(self.control.status[0], 'auto')

    def test_failed_selected_auto_retry_does_not_clobber_same_id_takeover(self):
        self.control.set_manual_fans({1: 2900})
        self.write_error = OSError(errno.EBUSY, 'busy')
        with self.assertRaises(OSError):
            self.control.set_auto_fans((1,))
        self.write_error = None
        (self.path / 'fan_state').write_text('1 0 manual 3400\n2 0 auto 0\n3 0 auto 0\n4 0 auto 0\n')
        self.control.check_status()
        self.assertEqual(self.selected_writes, ['manual 1:2900', 'auto 1'])
        self.assertEqual(self.control.status[0], 'cancelled')
        self.assertIn('IDs 1', self.control.status[3])
        self.assertEqual(self.control._selected_owned, {})
        self.assertEqual(self.device.persistence.get('FANCONTROL', 'fan_mode'), 'auto')

    def test_failed_selected_auto_preserves_takeover_reason_with_remaining_monitor(self):
        self.control.set_manual_fans({1: 2900, 3: 3000})
        self.write_error = OSError(errno.EBUSY, 'busy')
        with self.assertRaises(OSError):
            self.control.set_auto_fans((3,))
        self.write_error = None
        (self.path / 'fan_state').write_text('1 0 manual 2900\n2 0 auto 0\n3 0 manual 3400\n4 0 auto 0\n')
        self.control.check_status()
        self.assertEqual(self.selected_writes, ['manual 1:2900,3:3000', 'auto 3'])
        self.assertEqual(self.control.status[0], 'settling')
        self.assertIn('IDs 3', self.control.status[3])
        self.assertEqual(self.control._selected_owned, {1: 2900})

    def test_failed_selected_auto_retry_waits_for_readable_state(self):
        self.control.set_manual_fans({1: 2900})
        self.write_error = OSError(errno.EBUSY, 'busy')
        with self.assertRaises(OSError):
            self.control.set_auto_fans((1,))
        self.write_error = None
        (self.path / 'fan_state').unlink()
        self.control.check_status()
        self.assertEqual(self.selected_writes, ['manual 1:2900', 'auto 1'])
        self.assertEqual(self.control.status[0], 'error')
        self.state('manual', 2900)
        self.control.check_status()
        self.assertEqual(self.selected_writes, ['manual 1:2900', 'auto 1', 'auto 1'])
        self.assertEqual(self.control.status[0], 'auto')

    def test_selected_saved_intent_requires_registered_files_and_opt_in(self):
        self.device.persistence.read_dict({'FANCONTROL': {'fan_mode': 'selective', 'fan_targets': '1:2900,3:3000', 'fan_performance_mode': '0'}})
        self.control.restore_preferences()
        self.assertEqual(self.selected_writes, [])
        self.device.config.set('Startup', 'restore_persistence', 'true')
        self.device.METHODS = []
        self.control.restore_preferences()
        self.assertEqual(self.selected_writes, [])
        self.device.METHODS = ['set_fan_manual_fans']
        (self.path / 'fan_control_select').unlink()
        self.control.restore_preferences()
        self.assertEqual(self.selected_writes, [])
        (self.path / 'fan_control_select').write_text('')
        self.control.restore_preferences()
        self.assertEqual(self.selected_writes, ['manual 1:2900,3:3000'])


if __name__ == '__main__':
    unittest.main()
