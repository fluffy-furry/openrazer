# SPDX-License-Identifier: GPL-2.0-or-later

"""Fan requests, monitoring and power transitions."""

import configparser
import os
import threading
import time

from openrazer_daemon.dbus_services.dbus_methods import fan
from openrazer_daemon.misc.fan_monitor import FanMonitor


class _FanIsolationError(RuntimeError):
    pass


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
        self._selected_targets = {}
        self._selected_owned = {}
        self._selected_expected_modes = {}
        self._target_targets = {}
        self._target_owned = {}
        self._target_base_rpm = None
        self._pending_auto_ids = ()
        self._pending_auto_state = {}
        self._external_note = ''
        self._policy_status = ('idle', 0, {}, '')

    def attach_power(self, monitor):
        self._power_monitor = monitor
        monitor.register(self)
        self.restore_preferences()

    @property
    def status(self):
        with self._lock:
            status = self._policy_status or self._monitor.status
            reason = status[3]
            if self._external_note and self._external_note not in reason:
                reason = (reason + '; ' if reason else '') + self._external_note
            return status[0], status[1], dict(status[2]), reason

    def _write(self, value):
        self._write_file('fan_control', value)

    def _write_file(self, name, value):
        payload = value.encode('ascii')
        descriptor = os.open(self._device.get_driver_path(name), os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC)
        try:
            if os.write(descriptor, payload) != len(payload):
                raise OSError('Short fan control write')
        finally:
            os.close(descriptor)

    def _write_selected(self, value):
        self._write_file('fan_control_select', value)

    def _write_targets(self, targets):
        command = ','.join(f'{fan_id}:{rpm}' for fan_id, rpm in sorted(targets.items()))
        self._write_file('fan_control_targets', command)

    def _fail_selected_isolation(self, reason):
        self._monitor.cancel(reason)
        self._owned = True
        self._uncertain = True
        self._requested_rpm = None
        self._requested_performance = None
        self._expected_modes = {}
        self._selected_targets = {}
        self._selected_owned = {}
        self._selected_expected_modes = {}
        self._target_targets = {}
        self._target_owned = {}
        self._target_base_rpm = None
        self._pending_auto_ids = ()
        self._pending_auto_state = {}
        self._external_note = ''
        self._recovery_reason = reason
        self._recovery_state = 'error'
        self._recovery_attempts = 1
        try:
            self._write('auto')
        except Exception as error:
            self._recovery_failed = True
            self._policy_status = ('error', 0, {}, reason + '; global automatic restoration failed: ' + str(error))
            self._device.logger.error('%s', self._policy_status[3])
        else:
            self._owned = False
            self._uncertain = False
            self._recovery_failed = False
            self._recovery_attempts = 0
            self._policy_status = ('error', 0, {}, reason + '; all fans restored to automatic control')
            self._device.logger.error('%s', self._policy_status[3])
        self._save_preference()
        raise _FanIsolationError(reason)

    def _write_selected_checked(self, value, selected):
        before = {fan_id: (performance, mode, rpm) for fan_id, performance, mode, rpm in fan.get_fan_state(self._device)}
        write_error = None
        try:
            self._write_selected(value)
        except Exception as error:
            write_error = error
        try:
            after = {fan_id: (performance, mode, rpm) for fan_id, performance, mode, rpm in fan.get_fan_state(self._device)}
        except Exception as error:
            self._fail_selected_isolation('Could not verify fan isolation after a selected write: ' + str(error))
        changed = sorted(fan_id for fan_id in before.keys() | after.keys()
                         if fan_id not in selected and before.get(fan_id) != after.get(fan_id))
        if changed or before.keys() != after.keys():
            self._fail_selected_isolation('Selected fan write changed nonselected fan state'
                                          + (': ' + ','.join(map(str, changed)) if changed else ''))
        if write_error is not None:
            raise write_error

    @staticmethod
    def _target_value(targets):
        values = set(targets.values())
        return values.pop() if len(values) == 1 else 0

    def _save_preference(self):
        if getattr(self._device, 'disable_persistence', False):
            return
        persistence = self._device.persistence
        section = self._device.storage_name
        if not persistence.has_section(section):
            persistence.add_section(section)
        persistence.set(section, 'fan_mode', 'targets' if self._target_targets else 'selective' if self._selected_targets else 'manual' if self._requested_rpm is not None else 'auto')
        persistence.remove_option(section, 'fan_targets')
        persistence.remove_option(section, 'fan_base_rpm')
        if self._target_targets or self._selected_targets:
            targets = self._target_targets or self._selected_targets
            persistence.set(section, 'fan_targets', ','.join(f'{fan_id}:{rpm}' for fan_id, rpm in sorted(targets.items())))
            persistence.remove_option(section, 'fan_rpm')
            persistence.set(section, 'fan_performance_mode', str(self._requested_performance))
            if self._target_targets:
                persistence.set(section, 'fan_base_rpm', str(self._target_base_rpm))
        elif self._requested_rpm is None:
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
        if self._selected_owned:
            self._automatic('Superseded by an all-fan request', 'cancelled')
            if self._selected_owned:
                raise RuntimeError('Previous selected fan request could not be released')
        self._pending_auto_ids = ()
        self._pending_auto_state = {}
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
        self._selected_targets = {}
        self._selected_expected_modes = {}
        self._target_targets = {}
        self._target_owned = {}
        self._target_base_rpm = None
        self._uncertain = False
        self._last_ownership_check = time.monotonic()
        self._external_note = ''
        self._policy_status = None

    def set_manual(self, rpm):
        with self._lock:
            self._manual(rpm)
            self._save_preference()

    def _manual_targets(self, targets, restoring=False):
        if 'set_fan_manual_targets' not in getattr(self._device, 'METHODS', ()):
            raise RuntimeError('Independent fan targets are not registered for this model')
        if self._closed:
            raise RuntimeError('Fan control is closed')
        if self._sleeping:
            raise RuntimeError('Manual fan control is unavailable during system sleep')
        if self._power_monitor is None or self._power_monitor.power != 'ac':
            raise RuntimeError('Manual fan control requires confirmed AC power')
        allowed = set(fan.get_fan_target_ids(self._device))
        if not targets.keys() <= allowed:
            raise ValueError('Fan ID does not support independent targets')
        _, manual_modes, monitored_mask = fan.get_fan_config(self._device)
        minimum, default, maximum = fan.get_fan_limits(self._device)
        if any(not minimum <= rpm <= maximum for rpm in targets.values()):
            raise ValueError('Fan RPM is outside the current limits')
        if not allowed <= {fan_id for fan_id, _, _, _ in fan.get_fan_state(self._device)}:
            raise RuntimeError('Targetable fan identities are unavailable')
        if self._owned:
            ownership = self._ownership()
            if ownership == 'external':
                self._relinquish('Fan mode or target changed outside this request')
                raise RuntimeError('Fan mode or target changed outside this request')
            if ownership == 'unknown':
                raise RuntimeError('Could not verify the active fan setting')
            if ownership == 'auto':
                self._owned = False
                self._target_owned = {}
        rows = fan.get_fan_state(self._device)
        modes = {fan_id: performance for fan_id, performance, _, _ in rows}
        if not allowed <= modes.keys():
            raise RuntimeError('Targetable fan identities are unavailable')
        if not modes or len(set(modes.values())) != 1 or any(not manual_modes & (1 << mode) for mode in modes.values()):
            raise RuntimeError('Manual fan control is unavailable in the current performance mode')
        if restoring and self._requested_performance is not None and set(modes.values()) != {self._requested_performance}:
            raise RuntimeError('Saved performance mode is no longer active')
        if not targets.keys() <= modes.keys():
            raise RuntimeError('Fan identities changed before the manual request')
        if self._selected_owned:
            raise RuntimeError('Previous selected fan request must be released first')
        all_auto = all(mode == 'auto' for _, _, mode, _ in rows)
        all_manual = all(mode == 'manual' for _, _, mode, _ in rows)
        if restoring and not all_auto:
            raise RuntimeError('Another manual fan setting is active; saved control was not restored')
        if not all_auto and not (all_manual and self._owned and self._ownership() == 'owned'):
            raise RuntimeError('Another manual fan setting is active')
        if self._power_monitor.power != 'ac':
            raise RuntimeError('AC power changed before the manual request')
        baseline = self._target_base_rpm if restoring or self._target_owned else self._requested_rpm if self._owned else default
        if not minimum <= baseline <= maximum or baseline % 100:
            raise ValueError('Fan baseline RPM is outside the current limits')
        effective = {fan_id: baseline for fan_id in modes} if all_auto else {fan_id: rpm for fan_id, _, _, rpm in rows}
        effective.update(targets)
        monitored = tuple(fan_id for fan_id in effective if monitored_mask & (1 << fan_id))
        if not monitored:
            raise RuntimeError('Required fan telemetry is unavailable')
        self._monitor.cancel('Superseded by a target request')
        self._owned = True
        self._uncertain = True
        self._recovery_failed = False
        self._recovery_attempts = 0
        try:
            if all_auto:
                self._write(str(baseline))
                initialized = fan.get_fan_state(self._device)
                if {(fan_id, performance, mode, rpm) for fan_id, performance, mode, rpm in initialized} != {
                        (fan_id, performance, 'manual', baseline) for fan_id, performance in modes.items()}:
                    raise RuntimeError('Global manual initialization changed fan state unexpectedly')
            self._write_targets(targets)
            after = fan.get_fan_state(self._device)
            if {(fan_id, performance, mode, rpm) for fan_id, performance, mode, rpm in after} != {
                    (fan_id, performance, 'manual', effective[fan_id]) for fan_id, performance in modes.items()}:
                raise RuntimeError('Independent target write changed fan state unexpectedly')
            if self._power_monitor.power != 'ac':
                raise RuntimeError('AC power changed during the manual request')
            monitored_targets = {fan_id: effective[fan_id] for fan_id in monitored}
            self._monitor.start_targets(monitored_targets, monitored, {fan_id: modes[fan_id] for fan_id in monitored})
        except Exception:
            self._automatic('Manual target request failed', 'error')
            raise
        self._target_targets = {fan_id: effective[fan_id] for fan_id in allowed}
        self._target_owned = effective
        self._target_base_rpm = baseline
        self._requested_rpm = None
        self._requested_performance = next(iter(modes.values()))
        self._expected_modes = modes
        self._selected_targets = {}
        self._selected_expected_modes = {}
        self._uncertain = False
        self._last_ownership_check = time.monotonic()
        self._external_note = ''
        self._policy_status = None

    def set_manual_targets(self, targets):
        if not isinstance(targets, dict) or not targets:
            raise ValueError('Fan targets must be a nonempty mapping')
        if any(isinstance(fan_id, bool) or not isinstance(fan_id, int) or not 0 < fan_id <= 255 for fan_id in targets):
            raise ValueError('Invalid fan ID')
        if any(isinstance(rpm, bool) or not isinstance(rpm, int) or not 0 < rpm <= 25500 or rpm % 100 for rpm in targets.values()):
            raise ValueError('Fan RPM must be a positive multiple of 100')
        with self._lock:
            self._manual_targets(dict(targets))
            self._save_preference()

    def _manual_fans(self, targets, restoring=False):
        if self._closed:
            raise RuntimeError('Fan control is closed')
        if self._sleeping:
            raise RuntimeError('Manual fan control is unavailable during system sleep')
        if self._power_monitor is None or self._power_monitor.power != 'ac':
            raise RuntimeError('Manual fan control requires confirmed AC power')
        _, manual_modes, monitored_mask = fan.get_fan_config(self._device)
        rows = fan.get_fan_state(self._device)
        modes = {fan_id: performance for fan_id, performance, _, _ in rows}
        if not targets.keys() <= modes.keys():
            raise ValueError('Unknown fan ID')
        selected_modes = {fan_id: modes[fan_id] for fan_id in targets}
        if len(set(selected_modes.values())) != 1 or any(not manual_modes & (1 << mode) for mode in selected_modes.values()):
            raise RuntimeError('Manual fan control is unavailable in the current performance mode')
        minimum, _, maximum = fan.get_fan_limits(self._device)
        if any(not minimum <= rpm <= maximum for rpm in targets.values()):
            raise ValueError('Fan RPM is outside the current limits')
        if restoring:
            if any(mode != 'auto' for fan_id, _, mode, _ in rows if fan_id in targets):
                reason = 'Another manual fan setting is active; saved control was not restored'
                self._relinquish(reason)
                raise RuntimeError(reason)
            if self._requested_performance is not None and any(mode != self._requested_performance for mode in selected_modes.values()):
                raise RuntimeError('Saved performance mode is no longer active')
        if self._power_monitor.power != 'ac':
            raise RuntimeError('AC power changed before the manual request')
        if self._owned or self._selected_owned:
            self._automatic('Superseded by a selected fan request', 'cancelled')
            if self._owned or self._selected_owned:
                raise RuntimeError('Previous fan request could not be released')
        self._pending_auto_ids = ()
        self._pending_auto_state = {}
        rows = fan.get_fan_state(self._device)
        if any(mode != 'auto' for fan_id, _, mode, _ in rows if fan_id in targets):
            raise RuntimeError('Another manual fan setting is active for a selected fan')
        modes = {fan_id: performance for fan_id, performance, _, _ in rows}
        if not targets.keys() <= modes.keys():
            raise RuntimeError('Fan identities changed before the manual request')
        selected_modes = {fan_id: modes[fan_id] for fan_id in targets}
        if len(set(selected_modes.values())) != 1 or any(not manual_modes & (1 << mode) for mode in selected_modes.values()):
            raise RuntimeError('Fan identities or performance mode changed before the manual request')
        monitored = tuple(fan_id for fan_id in targets if monitored_mask & (1 << fan_id))
        self._monitor.cancel('Superseded by a selected fan request')
        self._selected_owned = dict(targets)
        self._selected_expected_modes = {fan_id: modes[fan_id] for fan_id in targets}
        self._uncertain = True
        self._recovery_failed = False
        self._recovery_attempts = 0
        try:
            command = 'manual ' + ','.join(f'{fan_id}:{targets[fan_id]}' for fan_id in sorted(targets))
            self._write_selected_checked(command, targets)
            if self._power_monitor.power != 'ac':
                raise RuntimeError('AC power changed during the manual request')
            self._monitor.start_targets(targets, monitored, self._selected_expected_modes)
        except _FanIsolationError:
            raise
        except Exception:
            self._selected_automatic('Manual request failed', 'error')
            raise
        self._selected_targets = dict(targets)
        self._requested_rpm = None
        self._requested_performance = next(iter(selected_modes.values()))
        self._expected_modes = {}
        self._uncertain = False
        self._last_ownership_check = time.monotonic()
        self._external_note = ''
        self._policy_status = None

    def set_manual_fans(self, targets):
        if not isinstance(targets, dict) or not targets:
            raise ValueError('Fan targets must be a nonempty mapping')
        if any(isinstance(fan_id, bool) or not isinstance(fan_id, int) or not 0 < fan_id <= 255 for fan_id in targets):
            raise ValueError('Invalid fan ID')
        if any(isinstance(rpm, bool) or not isinstance(rpm, int) or not 0 < rpm <= 25500 or rpm % 100 for rpm in targets.values()):
            raise ValueError('Fan RPM must be a positive multiple of 100')
        with self._lock:
            self._manual_fans(dict(targets))
            self._save_preference()

    def set_auto_fans(self, fan_ids):
        fan_ids = tuple(fan_ids)
        if not fan_ids or len(set(fan_ids)) != len(fan_ids) or any(isinstance(fan_id, bool) or not isinstance(fan_id, int) or not 0 < fan_id <= 255 for fan_id in fan_ids):
            raise ValueError('Invalid or duplicate fan ID')
        with self._lock:
            if self._closed:
                raise RuntimeError('Fan control is closed')
            if self._pending_auto_ids:
                self._retry_selected_auto()
                if self._pending_auto_ids:
                    raise RuntimeError('Previous automatic fan request could not be verified')
            self._external_note = ''
            rows = fan.get_fan_state(self._device)
            if not set(fan_ids) <= {fan_id for fan_id, _, _, _ in rows}:
                raise ValueError('Unknown fan ID')
            before = {fan_id: (performance, mode, rpm) for fan_id, performance, mode, rpm in rows if fan_id in fan_ids}
            if self._owned:
                if self._ownership() != 'owned':
                    self._relinquish('Fan mode or target changed outside this request')
                else:
                    remaining = {fan_id: self._requested_rpm for fan_id in self._expected_modes if fan_id not in fan_ids}
                    self._selected_owned = {fan_id: self._requested_rpm for fan_id in self._expected_modes}
                    self._selected_targets = dict(remaining)
                    self._selected_expected_modes = dict(self._expected_modes)
                    self._owned = False
                    self._requested_rpm = None
            self._monitor.cancel('Automatic control requested for selected fans')
            command = 'auto ' + ','.join(str(fan_id) for fan_id in sorted(fan_ids))
            try:
                self._write_selected_checked(command, fan_ids)
            except _FanIsolationError:
                raise
            except Exception as error:
                self._recovery_failed = True
                self._recovery_attempts = 1
                self._recovery_reason = 'Automatic control requested for selected fans'
                self._recovery_state = 'auto'
                self._pending_auto_ids = fan_ids
                self._pending_auto_state = before
                self._policy_status = ('error', 0, {}, str(error))
                raise
            self._pending_auto_ids = ()
            self._pending_auto_state = {}
            for fan_id in fan_ids:
                self._selected_owned.pop(fan_id, None)
                self._selected_targets.pop(fan_id, None)
                self._selected_expected_modes.pop(fan_id, None)
            if not self._selected_targets:
                self._requested_performance = None
            self._recovery_failed = False
            self._save_preference()
            if self._selected_owned:
                try:
                    _, _, monitored_mask = fan.get_fan_config(self._device)
                    monitored = tuple(fan_id for fan_id in self._selected_owned if monitored_mask & (1 << fan_id))
                    self._monitor.start_targets(self._selected_owned, monitored, self._selected_expected_modes)
                except Exception as error:
                    self._selected_automatic('Fan monitoring failed: ' + str(error), 'error')
                    raise
                self._policy_status = None
            else:
                self._policy_status = ('auto', 0, {}, '')

    def _selected_automatic(self, reason, state):
        previous = self._monitor.status
        self._monitor.cancel(reason)
        self._pending_auto_ids = ()
        self._pending_auto_state = {}
        target = self._target_value(self._selected_targets or self._selected_owned)
        self._policy_status = (state, target, previous[2], reason)
        if not self._selected_owned:
            return
        restore = dict(self._selected_owned)
        if not self._uncertain:
            try:
                rows = {fan_id: (performance, mode, rpm) for fan_id, performance, mode, rpm in fan.get_fan_state(self._device)}
            except Exception:
                rows = None
            if rows is not None:
                restore = {fan_id: rpm for fan_id, rpm in restore.items() if fan_id not in rows or rows[fan_id] == (self._selected_expected_modes[fan_id], 'manual', rpm)}
                external = {fan_id for fan_id in self._selected_owned if fan_id in rows and rows[fan_id] != (self._selected_expected_modes[fan_id], 'manual', self._selected_owned[fan_id]) and rows[fan_id][1] != 'auto'}
                for fan_id in external:
                    self._selected_targets.pop(fan_id, None)
                if external:
                    self._save_preference()
        self._selected_owned = restore
        if not restore:
            self._uncertain = False
            self._recovery_failed = False
            self._recovery_attempts = 0
            return
        self._recovery_reason = reason
        self._recovery_state = state
        self._recovery_attempts += 1
        try:
            self._write_selected_checked('auto ' + ','.join(str(fan_id) for fan_id in sorted(restore)), restore)
            self._selected_owned = {}
            self._uncertain = False
            self._recovery_failed = False
            self._recovery_attempts = 0
        except _FanIsolationError:
            return
        except Exception as error:
            self._recovery_failed = True
            self._policy_status = ('error', target, previous[2], reason + ': automatic restoration failed: ' + str(error))
            self._device.logger.error('Could not restore automatic fan control: %s', error)

    def _retry_selected_auto(self):
        if not self._pending_auto_ids:
            return
        self._recovery_attempts += 1
        try:
            current = {fan_id: (performance, mode, rpm) for fan_id, performance, mode, rpm in fan.get_fan_state(self._device)}
        except Exception as error:
            self._policy_status = ('error', 0, {}, 'Could not verify selected fan state before automatic retry: ' + str(error))
            self._device.logger.error('Could not verify selected fan state before automatic retry: %s', error)
            return
        retry = []
        finished = []
        external = []
        for fan_id in self._pending_auto_ids:
            state = current.get(fan_id)
            if state is not None and state[1] == 'auto':
                finished.append(fan_id)
            elif state == self._pending_auto_state[fan_id]:
                retry.append(fan_id)
            else:
                external.append(fan_id)
        for fan_id in (*finished, *external):
            self._selected_owned.pop(fan_id, None)
            self._selected_targets.pop(fan_id, None)
            self._selected_expected_modes.pop(fan_id, None)
        if external:
            self._external_note = 'Selected fan control changed outside this request for IDs ' + ','.join(map(str, sorted(external)))
            self._device.logger.warning('%s', self._external_note)
        self._pending_auto_ids = tuple(retry)
        self._pending_auto_state = {fan_id: self._pending_auto_state[fan_id] for fan_id in retry}
        if retry:
            try:
                self._write_selected_checked('auto ' + ','.join(str(fan_id) for fan_id in sorted(retry)), retry)
            except _FanIsolationError:
                return
            except Exception as error:
                self._save_preference()
                self._policy_status = ('error', 0, {}, 'Automatic control requested for selected fans: ' + str(error))
                self._device.logger.error('Could not restore automatic fan control: %s', error)
                return
            for fan_id in retry:
                self._selected_owned.pop(fan_id, None)
                self._selected_targets.pop(fan_id, None)
                self._selected_expected_modes.pop(fan_id, None)
        self._pending_auto_ids = ()
        self._pending_auto_state = {}
        self._recovery_failed = False
        self._recovery_attempts = 0
        if not self._selected_targets:
            self._requested_performance = None
        self._save_preference()
        if self._selected_owned:
            try:
                _, _, monitored_mask = fan.get_fan_config(self._device)
                monitored = tuple(fan_id for fan_id in self._selected_owned if monitored_mask & (1 << fan_id))
                self._monitor.start_targets(self._selected_owned, monitored, self._selected_expected_modes)
            except Exception as error:
                self._selected_automatic('Fan monitoring failed: ' + str(error), 'error')
                return
            self._policy_status = None
        else:
            self._policy_status = ('cancelled' if external else 'auto', 0, {},
                                   'Selected fan control changed outside this request' if external else '')

    def set_auto(self):
        with self._lock:
            if self._closed:
                raise RuntimeError('Fan control is closed')
            if self._selected_owned:
                self._automatic('Automatic control requested', 'auto')
                if self._selected_owned:
                    raise RuntimeError('Previous selected fan request could not be released')
            self._requested_rpm = None
            self._requested_performance = None
            self._expected_modes = {}
            self._selected_targets = {}
            self._selected_expected_modes = {}
            self._target_targets = {}
            self._target_base_rpm = None
            self._pending_auto_ids = ()
            self._pending_auto_state = {}
            self._external_note = ''
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
            self._selected_owned = {}
            self._target_owned = {}
            self._uncertain = False
            self._recovery_failed = False
            self._recovery_attempts = 0
            self._policy_status = ('auto', 0, {}, '')

    def _automatic(self, reason, state='suspended'):
        if self._selected_owned:
            self._selected_automatic(reason, state)
            return
        if self._pending_auto_ids:
            self._retry_selected_auto()
            return
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
                self._target_owned = {}
                return
        if reason != self._recovery_reason or not self._recovery_failed:
            self._recovery_attempts = 0
        self._recovery_reason = reason
        self._recovery_state = state
        self._recovery_attempts += 1
        try:
            self._write('auto')
            self._owned = False
            self._target_owned = {}
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
        if self._target_owned:
            if all(performance == self._expected_modes[fan_id] and mode == 'manual' and rpm == self._target_owned[fan_id]
                   for fan_id, performance, mode, rpm in state):
                return 'owned'
            return 'external'
        if all(performance == self._expected_modes[fan_id] and mode == 'manual' and rpm == self._requested_rpm for fan_id, performance, mode, rpm in state):
            return 'owned'
        return 'external'

    def _relinquish(self, reason):
        self._owned = False
        self._requested_rpm = None
        self._requested_performance = None
        self._expected_modes = {}
        self._selected_targets = {}
        self._selected_owned = {}
        self._selected_expected_modes = {}
        self._target_targets = {}
        self._target_owned = {}
        self._target_base_rpm = None
        self._pending_auto_ids = ()
        self._pending_auto_state = {}
        self._external_note = ''
        self._monitor.cancel(reason)
        self._policy_status = ('cancelled', 0, self._monitor.status[2], reason)
        self._save_preference()

    def _restore_request(self):
        if self._target_targets:
            if self._sleeping or self._closed or self._owned:
                return
            try:
                self._manual_targets(dict(self._target_targets), restoring=True)
            except Exception as error:
                if not self._recovery_failed and self._target_targets:
                    self._policy_status = ('suspended', self._target_value(self._target_targets), self._monitor.status[2], str(error))
                self._device.logger.warning('Manual fan targets were not restored: %s', error)
            return
        if self._selected_targets:
            if self._sleeping or self._closed or self._selected_owned:
                return
            try:
                self._manual_fans(dict(self._selected_targets), restoring=True)
            except Exception as error:
                if not self._recovery_failed and self._selected_targets:
                    self._policy_status = ('suspended', self._target_value(self._selected_targets), self._monitor.status[2], str(error))
                self._device.logger.warning('Selected manual fan control was not restored: %s', error)
            return
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
            if power != 'ac' and (self._owned or self._selected_owned or self._pending_auto_ids) and (previous != power or not self._recovery_failed):
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
                if self._owned or self._selected_owned or self._pending_auto_ids:
                    self._automatic('System is entering sleep')
            else:
                self._restore_request()

    def check_status(self):
        with self._lock:
            if self._closed or not (self._owned or self._selected_owned or self._pending_auto_ids):
                return
            if self._recovery_failed:
                if self._recovery_attempts < 3:
                    if self._pending_auto_ids:
                        self._retry_selected_auto()
                    else:
                        self._automatic(self._recovery_reason, self._recovery_state)
                return
            state, _, _, reason = self._policy_status or self._monitor.status
            if state == 'error':
                self._automatic('Fan monitoring failed: ' + reason, 'error')
            elif state == 'cancelled':
                if self._selected_owned:
                    self._automatic(reason, 'cancelled')
                    self._selected_targets = {}
                    self._requested_performance = None
                    self._save_preference()
                else:
                    self._relinquish(reason)
            elif state in ('reached', 'timeout', 'accepted', 'partially_reached', 'partially_timeout') and time.monotonic() - self._last_ownership_check >= 5:
                self._last_ownership_check = time.monotonic()
                if self._selected_owned:
                    try:
                        rows = {fan_id: (performance, mode, rpm) for fan_id, performance, mode, rpm in fan.get_fan_state(self._device)}
                    except Exception:
                        self._automatic('Could not verify the active fan setting', 'error')
                        return
                    if any(fan_id not in rows or rows[fan_id] != (self._selected_expected_modes[fan_id], 'manual', rpm) for fan_id, rpm in self._selected_owned.items()):
                        self._automatic('Fan mode or target changed outside this request', 'cancelled')
                        self._selected_targets = {}
                        self._requested_performance = None
                        self._save_preference()
                    return
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
            saved_mode = persistence.get(section, 'fan_mode', fallback='auto')
            if saved_mode == 'targets':
                if 'set_fan_manual_targets' not in getattr(self._device, 'METHODS', ()):
                    return
                try:
                    available = all(os.path.isfile(self._device.get_driver_path(name)) for name in ('fan_target_ids', 'fan_control_targets'))
                except (OSError, ValueError):
                    return
                if not available:
                    return
                try:
                    serialized = persistence.get(section, 'fan_targets')
                    entries = serialized.split(',')
                    if not entries or any(not entry or ':' not in entry for entry in entries):
                        raise ValueError('Invalid saved fan targets')
                    targets = {}
                    for entry in entries:
                        fan_id_text, rpm_text = entry.split(':', 1)
                        if not fan_id_text.isascii() or not fan_id_text.isdecimal() or not rpm_text.isascii() or not rpm_text.isdecimal():
                            raise ValueError('Invalid saved fan targets')
                        fan_id, rpm = int(fan_id_text), int(rpm_text)
                        if not 0 < fan_id <= 255 or fan_id in targets or not 0 < rpm <= 25500 or rpm % 100:
                            raise ValueError('Invalid saved fan targets')
                        targets[fan_id] = rpm
                    performance = persistence.getint(section, 'fan_performance_mode')
                    if not 0 <= performance < 32:
                        raise ValueError('Invalid saved fan performance mode')
                    baseline = persistence.getint(section, 'fan_base_rpm')
                    if not 0 < baseline <= 25500 or baseline % 100:
                        raise ValueError('Invalid saved fan baseline RPM')
                except (ValueError, configparser.Error) as error:
                    self._device.logger.warning('Ignoring invalid fan target preference: %s', error)
                    return
                self._target_targets = targets
                self._target_base_rpm = baseline
                self._requested_performance = performance
                self._policy_status = ('suspended', self._target_value(targets), {}, 'Waiting for AC power')
                if self._power == 'ac':
                    self._restore_request()
                return
            if saved_mode == 'selective':
                if 'set_fan_manual_fans' not in getattr(self._device, 'METHODS', ()):
                    return
                try:
                    available = all(os.path.isfile(self._device.get_driver_path(name)) for name in ('fan_groups', 'fan_control_select'))
                except (OSError, ValueError):
                    return
                if not available:
                    return
                try:
                    serialized = persistence.get(section, 'fan_targets')
                    entries = serialized.split(',')
                    if not entries or any(not entry or ':' not in entry for entry in entries):
                        raise ValueError('Invalid saved selected fan targets')
                    targets = {}
                    for entry in entries:
                        fan_id_text, rpm_text = entry.split(':', 1)
                        if not fan_id_text.isascii() or not fan_id_text.isdecimal() or not rpm_text.isascii() or not rpm_text.isdecimal():
                            raise ValueError('Invalid saved selected fan targets')
                        fan_id, rpm = int(fan_id_text), int(rpm_text)
                        if not 0 < fan_id <= 255 or fan_id in targets or not 0 < rpm <= 25500 or rpm % 100:
                            raise ValueError('Invalid saved selected fan targets')
                        targets[fan_id] = rpm
                    performance = persistence.getint(section, 'fan_performance_mode')
                    if not 0 <= performance < 32:
                        raise ValueError('Invalid saved fan performance mode')
                except (ValueError, configparser.Error) as error:
                    self._device.logger.warning('Ignoring invalid selected fan preference: %s', error)
                    return
                self._selected_targets = targets
                self._requested_performance = performance
                self._policy_status = ('suspended', self._target_value(targets), {}, 'Waiting for AC power')
                if self._power == 'ac':
                    self._restore_request()
                return
            if saved_mode != 'manual':
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
