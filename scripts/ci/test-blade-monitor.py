#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later

from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'daemon'))

from openrazer_daemon.misc.fan_monitor import FanMonitor


class FanMonitorTests(unittest.TestCase):
    def setUp(self):
        self.rows = [(fan_id, 0, 'manual', 2900) for fan_id in (1, 2, 3, 4)]
        self.speeds = {1: 2900, 2: 2900, 3: 0, 4: 0}
        self.read_state = Mock(side_effect=lambda: list(self.rows))
        self.read_rpm = Mock(side_effect=lambda: dict(self.speeds))
        self.logger = Mock()
        self.modes = {fan_id: 0 for fan_id in (1, 2, 3, 4)}

    def monitor(self, **kwargs):
        monitor = FanMonitor(self.read_state, self.read_rpm, self.logger, interval=0.005, **kwargs)
        self.addCleanup(monitor.close)
        return monitor

    def start(self, monitor):
        monitor.start(2900, (1, 2), self.modes)

    def wait_status(self, monitor, state):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = monitor.status
            if status[0] == state:
                return status
            threading.Event().wait(0.002)
        self.fail('Expected {0}, got {1}'.format(state, monitor.status))

    def test_idle_does_not_read(self):
        monitor = self.monitor()
        self.assertEqual(monitor.status, ('idle', 0, {}, ''))
        monitor.cancel('No request')
        self.assertEqual(monitor.status, ('idle', 0, {}, ''))
        self.read_state.assert_not_called()
        self.read_rpm.assert_not_called()

    def test_both_monitored_fans_must_reach_target(self):
        self.speeds[2] = 2800
        monitor = self.monitor(timeout=0.03)
        self.start(monitor)
        status = self.wait_status(monitor, 'timeout')
        self.assertEqual(status[1:3], (2900, self.speeds))
        self.assertGreater(self.read_rpm.call_count, 0)

    def test_auxiliary_fans_do_not_block_convergence(self):
        monitor = self.monitor()
        self.start(monitor)
        self.assertEqual(self.wait_status(monitor, 'reached'), ('reached', 2900, self.speeds, ''))
        count = self.read_rpm.call_count
        threading.Event().wait(0.02)
        self.assertEqual(self.read_rpm.call_count, count)

    def test_status_is_a_copy_and_never_reads(self):
        monitor = self.monitor()
        self.start(monitor)
        self.wait_status(monitor, 'reached')
        self.read_state.reset_mock()
        self.read_rpm.reset_mock()
        monitor.status[2][1] = 0
        self.assertEqual(monitor.status[2], self.speeds)
        self.read_state.assert_not_called()
        self.read_rpm.assert_not_called()

    def test_mode_or_target_change_cancels_all_fan_ids(self):
        for row in ((3, 0, 'auto', 0), (4, 6, 'manual', 2900), (1, 0, 'manual', 3000)):
            with self.subTest(row=row):
                self.rows = [(fan_id, 0, 'manual', 2900) for fan_id in (1, 2, 3, 4)]
                self.rows[row[0] - 1] = row
                self.read_rpm.reset_mock()
                monitor = self.monitor()
                self.start(monitor)
                self.wait_status(monitor, 'cancelled')
                self.read_rpm.assert_not_called()
                monitor.close()

    def test_missing_state_fan_cancels(self):
        self.rows.pop()
        monitor = self.monitor()
        self.start(monitor)
        self.assertEqual(self.wait_status(monitor, 'cancelled')[3], 'Available fans changed')
        self.read_rpm.assert_not_called()

    def test_missing_rpm_is_error_not_a_default_speed(self):
        del self.speeds[2]
        monitor = self.monitor()
        self.start(monitor)
        status = self.wait_status(monitor, 'error')
        self.assertEqual(status[2], {})
        self.assertIn('Missing', status[3])

    def test_read_errors_are_terminal_and_preserved(self):
        for error in (OSError('Device gone'), ValueError('Invalid row'), RuntimeError('Reader failed')):
            with self.subTest(error=error):
                self.read_state.side_effect = error
                monitor = self.monitor()
                self.start(monitor)
                self.assertEqual(self.wait_status(monitor, 'error'), ('error', 2900, {}, str(error)))
                self.read_rpm.assert_not_called()
                monitor.close()

    def test_invalid_telemetry_is_error(self):
        for rpm in (-1, 2950, True, 25600, '2900'):
            with self.subTest(rpm=rpm):
                self.speeds[1] = rpm
                monitor = self.monitor()
                self.start(monitor)
                self.wait_status(monitor, 'error')
                monitor.close()

    def test_cancel_during_state_read_skips_followup_rpm(self):
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()

        def read_state():
            entered.set()
            release.wait(2)
            completed.set()
            return self.rows

        self.read_state.side_effect = read_state
        self.addCleanup(release.set)
        monitor = self.monitor()
        self.start(monitor)
        self.assertTrue(entered.wait(1))
        monitor.cancel('Automatic control requested')
        release.set()
        self.assertTrue(completed.wait(1))
        threading.Event().wait(0.01)
        self.assertEqual(monitor.status, ('cancelled', 2900, {}, 'Automatic control requested'))
        self.read_rpm.assert_not_called()

    def test_superseding_request_discards_old_rpm_result(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def read_rpm():
            calls.append(True)
            if len(calls) == 1:
                entered.set()
                release.wait(2)
                return {1: 2900, 2: 2900, 3: 0, 4: 0}
            return {1: 3000, 2: 3000, 3: 0, 4: 0}

        self.read_rpm.side_effect = read_rpm
        self.addCleanup(release.set)
        monitor = self.monitor()
        self.start(monitor)
        self.assertTrue(entered.wait(1))
        self.rows = [(fan_id, 0, 'manual', 3000) for fan_id in (1, 2, 3, 4)]
        monitor.start(3000, (1, 2), self.modes)
        self.assertEqual(monitor.status, ('settling', 3000, {}, ''))
        release.set()
        self.assertEqual(self.wait_status(monitor, 'reached'), ('reached', 3000, {1: 3000, 2: 3000, 3: 0, 4: 0}, ''))
        self.assertEqual(len(calls), 2)

    def test_close_does_not_wait_for_slow_io_or_publish_late(self):
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()

        def read_rpm():
            entered.set()
            release.wait(2)
            completed.set()
            return self.speeds

        self.read_rpm.side_effect = read_rpm
        self.addCleanup(release.set)
        monitor = self.monitor()
        self.start(monitor)
        self.assertTrue(entered.wait(1))
        before = time.monotonic()
        monitor.close()
        self.assertLess(time.monotonic() - before, 0.5)
        release.set()
        self.assertTrue(completed.wait(1))
        threading.Event().wait(0.01)
        self.assertEqual(monitor.status, ('cancelled', 2900, {}, 'Device closed'))
        with self.assertRaises(RuntimeError):
            self.start(monitor)

    def test_deadline_includes_time_spent_reading(self):
        offset = [0]
        monitor = self.monitor(timeout=90, clock=lambda: time.monotonic() + offset[0])

        def read_rpm():
            offset[0] = 91
            return self.speeds

        self.read_rpm.side_effect = read_rpm
        self.start(monitor)
        self.assertEqual(self.wait_status(monitor, 'timeout')[2], self.speeds)

    def test_cancel_invalidates_completed_status(self):
        monitor = self.monitor()
        self.start(monitor)
        self.wait_status(monitor, 'reached')
        monitor.cancel('Automatic control requested')
        self.assertEqual(monitor.status, ('cancelled', 2900, self.speeds, 'Automatic control requested'))

    def test_invalid_request_never_reads(self):
        monitor = self.monitor()
        for target, ids, modes in ((True, (1, 2), self.modes), (2950, (1, 2), self.modes), (2900, (), self.modes), (2900, (1, 1), self.modes), (2900, (1, 5), self.modes), (2900, (True,), self.modes), (2900, (1,), {1: 256})):
            with self.subTest(target=target, ids=ids, modes=modes):
                with self.assertRaises(ValueError):
                    monitor.start(target, ids, modes)
        self.assertEqual(monitor.status, ('idle', 0, {}, ''))
        self.read_state.assert_not_called()
        self.read_rpm.assert_not_called()


if __name__ == '__main__':
    unittest.main()
