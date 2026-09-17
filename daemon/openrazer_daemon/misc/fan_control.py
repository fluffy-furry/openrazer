# SPDX-License-Identifier: GPL-2.0-or-later

"""Fan requests, monitoring and power transitions."""

import configparser
import threading
import time

from openrazer_daemon.dbus_services.dbus_methods import fan
from openrazer_daemon.misc.fan_monitor import FanMonitor


class FanControl:
    def __init__(self, device):
        self._device = device
        self._lock = threading.RLock()
        self._monitor = FanMonitor(lambda: fan.get_fan_state(device), lambda: fan.get_fan_rpm(device), device.logger)
        self._power_monitor = None
        self._power = 'unknown'
        self._sleeping = False
        self._closed = False
        self._owned = False
        self._uncertain = False
        self._last_ownership_check = 0
        self._recovery_failed = False
        self._recovery_attempts = 0
        self._recovery_reason = ''
        self._recovery_state = 'error'
        self._requested_rpm = None
        self._requested_performance = None
        self._expected_modes = {}
        self._policy_status = ('idle', 0, {}, '')

    def attach_power(self, monitor):
        self._power_monitor = monitor
        monitor.register(self)
        self.restore_preferences()

    @property
    def status(self):
        with self._lock:
            status = self._policy_status or self._monitor.status
            return status[0], status[1], dict(status[2]), status[3]

    def _write(self, value):
        with open(self._device.get_driver_path('fan_control'), 'w') as driver_file:
            driver_file.write(value)

    def _save_preference(self):
        if getattr(self._device, 'disable_persistence', False):
            return
        persistence = self._device.persistence
        section = self._device.storage_name
        if not persistence.has_section(section):
            persistence.add_section(section)
        persistence.set(section, 'fan_mode', 'manual' if self._requested_rpm is not None else 'auto')
        if self._requested_rpm is None:
            persistence.remove_option(section, 'fan_rpm')
            persistence.remove_option(section, 'fan_performance_mode')
        else:
            persistence.set(section, 'fan_rpm', str(self._requested_rpm))
            if self._requested_performance is not None:
                persistence.set(section, 'fan_performance_mode', str(self._requested_performance))
        if hasattr(persistence, 'status'):
            persistence.status['changed'] = True

    def _manual(self, rpm, restoring=False):
        if isinstance(rpm, bool) or not isinstance(rpm, int) or not 0 < rpm <= 25500 or rpm % 100:
            raise ValueError('Fan RPM must be a positive multiple of 100')
        if self._closed:
            raise RuntimeError('Fan control is closed')
        if self._sleeping:
            raise RuntimeError('Manual fan control is unavailable during system sleep')
        power = self._power_monitor.power if self._power_monitor is not None else 'unknown'
        if power != 'ac':
            raise RuntimeError('Manual fan control requires confirmed AC power')
        _, manual_modes, monitored_fans = fan.get_fan_config(self._device)
        state = fan.get_fan_state(self._device)
        if restoring and any(mode != 'auto' for _, _, mode, _ in state):
            reason = 'Another manual fan setting is active; saved control was not restored'
            self._relinquish(reason)
            raise RuntimeError(reason)
        modes = {fan_id: performance for fan_id, performance, _, _ in state}
        if len(set(modes.values())) != 1 or any(not manual_modes & (1 << mode) for mode in modes.values()):
            raise RuntimeError('Manual fan control is unavailable in the current performance mode')
        if restoring and self._expected_modes and modes != self._expected_modes:
            raise RuntimeError('Performance mode or fan identities changed; manual control was not restored')
        if restoring and self._requested_performance is not None and any(mode != self._requested_performance for mode in modes.values()):
            raise RuntimeError('Saved performance mode is no longer active')
        ids = tuple(fan_id for fan_id in range(1, 32) if monitored_fans & (1 << fan_id))
        if not ids or not set(ids).issubset(modes):
            raise RuntimeError('Required fan telemetry is unavailable')
        minimum, _, maximum = fan.get_fan_limits(self._device)
        if not minimum <= rpm <= maximum:
            raise ValueError('Fan RPM is outside the current limits')
        if self._power_monitor.power != 'ac':
            raise RuntimeError('AC power changed before the manual request')
        self._monitor.cancel('Superseded by a manual request')
        self._owned = True
        self._uncertain = True
        self._recovery_failed = False
        self._recovery_attempts = 0
        try:
            self._write(str(rpm))
            if self._power_monitor.power != 'ac':
                raise RuntimeError('AC power changed during the manual request')
            self._monitor.start(rpm, ids, modes)
        except Exception:
            self._automatic('Manual request failed', 'error')
            raise
        self._power = power
        self._requested_rpm = rpm
        self._requested_performance = next(iter(modes.values()))
        self._expected_modes = modes
        self._uncertain = False
        self._last_ownership_check = time.monotonic()
        self._policy_status = None

    def set_manual(self, rpm):
        with self._lock:
            self._manual(rpm)
            self._save_preference()

    def set_auto(self):
        with self._lock:
            if self._closed:
                raise RuntimeError('Fan control is closed')
            self._requested_rpm = None
            self._requested_performance = None
            self._expected_modes = {}
            self._monitor.cancel('Automatic control requested')
            self._save_preference()
            try:
                self._write('auto')
            except Exception as error:
                self._owned = True
                self._recovery_failed = True
                self._recovery_attempts = 1
                self._recovery_reason = 'Automatic control requested'
                self._recovery_state = 'auto'
                self._policy_status = ('error', 0, {}, str(error))
                raise
            self._owned = False
            self._uncertain = False
            self._recovery_failed = False
            self._recovery_attempts = 0
            self._policy_status = ('auto', 0, {}, '')

    def _automatic(self, reason, state='suspended'):
        previous = self._monitor.status
        self._monitor.cancel(reason)
        self._policy_status = (state, self._requested_rpm or 0, previous[2], reason)
        if not self._owned:
            return
        if not self._uncertain and not self._recovery_failed:
            ownership = self._ownership()
            if ownership == 'external':
                self._relinquish('Fan mode or target changed outside this request')
                return
            if ownership == 'auto':
                self._owned = False
                return
        if reason != self._recovery_reason or not self._recovery_failed:
            self._recovery_attempts = 0
        self._recovery_reason = reason
        self._recovery_state = state
        self._recovery_attempts += 1
        try:
            self._write('auto')
            self._owned = False
            self._uncertain = False
            self._recovery_failed = False
            self._recovery_attempts = 0
        except Exception as error:
            self._recovery_failed = True
            self._policy_status = ('error', self._requested_rpm or 0, previous[2], reason + ': automatic restoration failed: ' + str(error))
            self._device.logger.error('Could not restore automatic fan control: %s', error)

    def _ownership(self):
        try:
            state = fan.get_fan_state(self._device)
        except Exception:
            return 'unknown'
        if {fan_id for fan_id, _, _, _ in state} != self._expected_modes.keys():
            return 'external'
        if all(mode == 'auto' for _, _, mode, _ in state):
            return 'auto'
        if all(performance == self._expected_modes[fan_id] and mode == 'manual' and rpm == self._requested_rpm for fan_id, performance, mode, rpm in state):
            return 'owned'
        return 'external'

    def _relinquish(self, reason):
        self._owned = False
        self._requested_rpm = None
        self._requested_performance = None
        self._expected_modes = {}
        self._monitor.cancel(reason)
        self._policy_status = ('cancelled', 0, self._monitor.status[2], reason)
        self._save_preference()

    def _restore_request(self):
        if self._requested_rpm is None or self._sleeping or self._closed or self._owned:
            return
        try:
            self._manual(self._requested_rpm, restoring=True)
        except Exception as error:
            if not self._recovery_failed and self._requested_rpm is not None:
                self._policy_status = ('suspended', self._requested_rpm, self._monitor.status[2], str(error))
            self._device.logger.warning('Manual fan control was not restored: %s', error)

    def update_power(self, power):
        with self._lock:
            if self._closed:
                return
            previous = self._power
            self._power = power
            if power != 'ac' and self._owned and (previous != power or not self._recovery_failed):
                self._automatic('AC power is unavailable' if power == 'battery' else 'Power source is unknown')
            elif power == 'ac' and previous != 'ac':
                self._restore_request()

    def prepare_for_sleep(self, sleeping):
        with self._lock:
            if self._closed:
                return
            if bool(sleeping) == self._sleeping:
                return
            self._sleeping = bool(sleeping)
            if self._sleeping:
                if self._owned:
                    self._automatic('System is entering sleep')
            else:
                self._restore_request()

    def check_status(self):
        with self._lock:
            if self._closed or not self._owned:
                return
            if self._recovery_failed:
                if self._recovery_attempts < 3:
                    self._automatic(self._recovery_reason, self._recovery_state)
                return
            state, _, _, reason = self._monitor.status
            if state == 'error':
                self._automatic('Fan monitoring failed: ' + reason, 'error')
            elif state == 'cancelled':
                self._relinquish(reason)
            elif state in ('reached', 'timeout') and time.monotonic() - self._last_ownership_check >= 5:
                self._last_ownership_check = time.monotonic()
                ownership = self._ownership()
                if ownership in ('auto', 'external'):
                    self._relinquish('Fan mode or target changed outside this request')
                elif ownership == 'unknown':
                    self._automatic('Could not verify the active fan setting', 'error')

    def restore_preferences(self):
        with self._lock:
            if not self._device.config.getboolean('Startup', 'restore_persistence', fallback=False):
                return
            persistence = self._device.persistence
            section = self._device.storage_name
            if persistence.get(section, 'fan_mode', fallback='auto') != 'manual':
                return
            try:
                rpm = persistence.getint(section, 'fan_rpm')
                if not 0 < rpm <= 25500 or rpm % 100:
                    raise ValueError('Invalid saved fan RPM')
                performance = persistence.getint(section, 'fan_performance_mode')
                if not 0 <= performance < 32:
                    raise ValueError('Invalid saved fan performance mode')
            except (ValueError, configparser.Error) as error:
                self._device.logger.warning('Ignoring invalid fan preference: %s', error)
                return
            self._requested_rpm = rpm
            self._requested_performance = performance
            self._policy_status = ('suspended', rpm, {}, 'Waiting for AC power')
            if self._power == 'ac':
                self._restore_request()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._automatic('Daemon is releasing fan control', 'cancelled')
            self._closed = True
            self._monitor.close()
            if self._power_monitor is not None:
                self._power_monitor.unregister(self)
