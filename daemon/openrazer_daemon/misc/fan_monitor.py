# SPDX-License-Identifier: GPL-2.0-or-later

"""Asynchronous confirmation of requested fan speeds."""

import threading
import time


class FanMonitor:
    """Observe fan speeds without changing device settings."""

    def __init__(self, read_state, read_rpm, logger, *, interval=1.0, timeout=90.0, clock=time.monotonic):
        if interval <= 0 or timeout <= 0:
            raise ValueError('Fan monitor timings must be positive')
        self._read_state = read_state
        self._read_rpm = read_rpm
        self._logger = logger
        self._interval = interval
        self._timeout = timeout
        self._clock = clock
        self._condition = threading.Condition()
        self._thread = None
        self._closed = False
        self._generation = 0
        self._request = None
        self._deadline = 0
        self._next_poll = 0
        self._status = ('idle', 0, {}, '')

    @property
    def status(self):
        """Return the last observed status without reading the device."""
        with self._condition:
            state, target, speeds, reason = self._status
            return state, target, dict(speeds), reason

    def start(self, target, monitored_ids, expected_modes):
        """Monitor an accepted manual request and its expected fan modes."""
        if isinstance(target, bool) or not isinstance(target, int) or not 0 < target <= 25500 or target % 100:
            raise ValueError('Invalid fan target RPM')
        monitored_ids = tuple(monitored_ids)
        if not monitored_ids:
            raise ValueError('Invalid monitored fan IDs')
        self._start({fan_id: target for fan_id in expected_modes}, monitored_ids, expected_modes, target, False)

    def start_targets(self, targets, monitored_ids, expected_modes):
        """Observe selected fans; an unmonitored request remains unverified."""
        targets = dict(targets)
        if not targets:
            raise ValueError('Invalid selected fan targets')
        target = next(iter(targets.values())) if len(set(targets.values())) == 1 else 0
        self._start(targets, monitored_ids, expected_modes, target, True)

    def _start(self, targets, monitored_ids, expected_modes, target, selective):
        targets = dict(targets)
        monitored_ids = tuple(monitored_ids)
        expected_modes = dict(expected_modes)
        if not targets or targets.keys() != expected_modes.keys():
            raise ValueError('Invalid selected fan targets')
        if len(set(monitored_ids)) != len(monitored_ids) or not set(monitored_ids) <= targets.keys():
            raise ValueError('Invalid monitored fan IDs')
        if any(isinstance(fan_id, bool) or not isinstance(fan_id, int) or not 0 < fan_id <= 255 for fan_id in (*expected_modes, *monitored_ids)):
            raise ValueError('Invalid expected fan ID')
        if any(isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 255 for mode in expected_modes.values()):
            raise ValueError('Invalid expected performance mode')
        if any(isinstance(rpm, bool) or not isinstance(rpm, int) or not 0 < rpm <= 25500 or rpm % 100 for rpm in targets.values()):
            raise ValueError('Invalid fan target RPM')
        missing = tuple(sorted(targets.keys() - set(monitored_ids))) if selective else ()
        detail = 'RPM telemetry unavailable for selected fan IDs ' + ','.join(map(str, missing)) if missing else ''
        if not target:
            detail = (detail + '; ' if detail else '') + 'Selected fans have different RPM targets'
        with self._condition:
            if self._closed:
                raise RuntimeError('Fan monitor is closed')
            now = self._clock()
            self._generation += 1
            self._request = targets, monitored_ids, expected_modes, target, selective
            self._deadline = now + self._timeout
            self._next_poll = now + self._interval
            if not monitored_ids:
                self._request = None
                self._status = ('accepted', target, {}, detail)
                return
            self._status = ('settling', target, {}, detail)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='razer-fan-monitor', daemon=True)
                self._thread.start()
            self._condition.notify_all()

    def _cancel(self, reason):
        self._generation += 1
        self._request = None
        state, target, speeds, _ = self._status
        if state != 'idle':
            self._status = ('cancelled', target, speeds, reason)
        self._condition.notify_all()

    def cancel(self, reason):
        """Discard the current request and any reads still in progress."""
        with self._condition:
            self._cancel(reason)

    def close(self):
        """Stop scheduling reads without waiting for device I/O."""
        with self._condition:
            if not self._closed:
                self._closed = True
                self._cancel('Device closed')

    def _sample(self, generation, targets, monitored_ids, expected_modes, target, selective):
        rows = self._read_state()
        seen = set()
        for fan_id, performance, mode, rpm in rows:
            if fan_id in seen:
                raise ValueError('Duplicate fan ID in state')
            seen.add(fan_id)
            if fan_id in targets and (performance != expected_modes[fan_id] or mode != 'manual' or rpm != targets[fan_id]):
                return 'cancelled', None, 'Fan mode or target changed'
        if (selective and not targets.keys() <= seen) or (not selective and seen != targets.keys()):
            return 'cancelled', None, 'Available fans changed'
        with self._condition:
            if generation != self._generation or self._closed:
                return 'cancelled', None, 'Request superseded'
        speeds = dict(self._read_rpm())
        if not set(monitored_ids) <= speeds.keys():
            raise ValueError('Missing monitored fan RPM')
        for speed in speeds.values():
            if isinstance(speed, bool) or not isinstance(speed, int) or not 0 <= speed <= 25500 or speed % 100:
                raise ValueError('Invalid current fan RPM')
        reached = all(abs(speeds[fan_id] - targets[fan_id]) < 100 for fan_id in monitored_ids)
        if selective:
            speeds = {fan_id: speeds[fan_id] for fan_id in monitored_ids}
        missing = sorted(targets.keys() - set(monitored_ids)) if selective else []
        detail = 'RPM telemetry unavailable for selected fan IDs ' + ','.join(map(str, missing)) if missing else ''
        if not target:
            detail = (detail + '; ' if detail else '') + 'Selected fans have different RPM targets'
        return ('partially_reached' if missing else 'reached') if reached else 'settling', speeds, detail

    def _run(self):
        while True:
            with self._condition:
                while not self._closed:
                    if self._request is None:
                        self._condition.wait()
                        continue
                    now = self._clock()
                    if now >= self._deadline:
                        _, target, speeds, _ = self._status
                        missing = set(self._request[0]) - set(self._request[1]) if self._request[4] else set()
                        phase = 'partially_timeout' if missing else 'timeout'
                        self._status = (phase, target, speeds, 'Fan speed did not settle before the deadline; RPM telemetry unavailable for selected fan IDs ' + ','.join(map(str, sorted(missing))) if missing else 'Fan speed did not settle before the deadline')
                        self._request = None
                        continue
                    delay = min(self._next_poll, self._deadline) - now
                    if delay > 0:
                        self._condition.wait(delay)
                        continue
                    generation = self._generation
                    request = self._request
                    break
                if self._closed:
                    return
            try:
                state, speeds, reason = self._sample(generation, *request)
            except Exception as error:
                state, speeds, reason = 'error', None, str(error)
            with self._condition:
                if generation != self._generation or self._closed:
                    continue
                target = request[3]
                if speeds is None:
                    speeds = self._status[2]
                if state in ('settling', 'reached', 'partially_reached') and self._clock() >= self._deadline:
                    missing = set(request[0]) - set(request[1]) if request[4] else set()
                    state = 'partially_timeout' if missing else 'timeout'
                    reason = 'Fan speed did not settle before the deadline; RPM telemetry unavailable for selected fan IDs ' + ','.join(map(str, sorted(missing))) if missing else 'Fan speed did not settle before the deadline'
                self._status = (state, target, speeds, reason)
                if state == 'settling':
                    self._next_poll = self._clock() + self._interval
                else:
                    self._request = None
            if state == 'error':
                self._logger.warning('Could not monitor fan speed: %s', reason)
