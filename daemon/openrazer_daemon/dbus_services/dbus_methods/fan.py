# SPDX-License-Identifier: GPL-2.0-or-later

"""Fan control methods."""

import re

from openrazer_daemon.dbus_services import endpoint


_FAN_FILES = ('fan_state', 'fan_rpm', 'fan_limits', 'fan_control', 'fan_modes', 'fan_rpm_monitor')
_FAN_SELECT_FILES = _FAN_FILES + ('fan_groups', 'fan_control_select')


def _read_fan_rows(device, filename, columns):
    with open(device.get_driver_path(filename), 'r') as driver_file:
        rows = [line.split() for line in driver_file]
    if not rows or any(len(row) != columns for row in rows):
        raise ValueError('Invalid ' + filename + ' response')
    return rows


def _fan_number(value, maximum):
    if not value.isascii() or not value.isdecimal():
        raise ValueError('Invalid fan value')
    number = int(value)
    if number > maximum:
        raise ValueError('Fan value out of range')
    return number


def _fan_id(value, seen):
    fan_id = _fan_number(value, 255)
    if fan_id == 0 or fan_id in seen:
        raise ValueError('Invalid or duplicate fan ID')
    seen.add(fan_id)
    return fan_id


@endpoint('razer.device.fan', 'getFanState', out_sig='a(yysq)', required_files=_FAN_FILES)
def get_fan_state(self):
    """Get each fan's ID, performance mode, control mode and target RPM."""
    self.logger.debug('DBus call get_fan_state')
    result = []
    seen = set()
    for fan_id, performance, mode, rpm in _read_fan_rows(self, 'fan_state', 4):
        if mode not in ('auto', 'manual'):
            raise ValueError('Invalid fan control mode')
        target = _fan_number(rpm, 25500)
        if target % 100 or (mode == 'manual' and not target):
            raise ValueError('Invalid fan target RPM')
        result.append((_fan_id(fan_id, seen), _fan_number(performance, 255), mode, target))
    return result


@endpoint('razer.device.fan', 'getFanRPM', out_sig='a{yq}', required_files=_FAN_FILES)
def get_fan_rpm(self):
    """Get current RPM reported by each fan."""
    self.logger.debug('DBus call get_fan_rpm')
    result = {}
    seen = set()
    for fan_id, rpm in _read_fan_rows(self, 'fan_rpm', 2):
        current = _fan_number(rpm, 25500)
        if current % 100:
            raise ValueError('Invalid fan current RPM')
        result[_fan_id(fan_id, seen)] = current
    return result


@endpoint('razer.device.fan', 'getFanLimits', out_sig='qqq', required_files=_FAN_FILES)
def get_fan_limits(self):
    """Get the current minimum, default and maximum manual RPM."""
    self.logger.debug('DBus call get_fan_limits')
    rows = _read_fan_rows(self, 'fan_limits', 3)
    if len(rows) != 1:
        raise ValueError('Invalid fan limits response')
    limits = tuple(_fan_number(value, 25500) for value in rows[0])
    if not 0 < limits[0] <= limits[1] <= limits[2] or any(value % 100 for value in limits):
        raise ValueError('Invalid fan RPM limits')
    return limits


@endpoint('razer.device.fan', 'setFanAuto', required_files=_FAN_FILES)
def set_fan_auto(self):
    """Return all fans to automatic control without changing performance mode."""
    self.logger.debug('DBus call set_fan_auto')
    self._fan_control.set_auto()


@endpoint('razer.device.fan', 'setFanManual', in_sig='q', required_files=_FAN_FILES)
def set_fan_manual(self, rpm):
    """Set all fans to manual control at the requested RPM."""
    self.logger.debug('DBus call set_fan_manual')
    if isinstance(rpm, bool) or not isinstance(rpm, int):
        raise TypeError('Fan RPM must be an integer')
    if not 0 < rpm <= 25500 or rpm % 100:
        raise ValueError('Fan RPM must be a positive multiple of 100')
    self._fan_control.set_manual(rpm)


@endpoint('razer.device.fan', 'getFanConfig', out_sig='uuu', required_files=_FAN_FILES)
def get_fan_config(self):
    """Get automatic-mode, manual-mode and monitored-fan bitmasks."""
    rows = _read_fan_rows(self, 'fan_modes', 2)
    monitored = _read_fan_rows(self, 'fan_rpm_monitor', 1)
    if len(rows) != 1 or len(monitored) != 1:
        raise ValueError('Invalid fan configuration')
    automatic, manual = (_fan_number(value, 0xFFFFFFFF) for value in rows[0])
    mask = _fan_number(monitored[0][0], 0xFFFFFFFF)
    if not automatic or manual & ~automatic or not mask or mask & 1:
        raise ValueError('Invalid fan configuration masks')
    return automatic, manual, mask


@endpoint('razer.device.fan', 'getFanStatus', out_sig='sqa{yq}s', required_files=_FAN_FILES)
def get_fan_status(self):
    """Get cached settling status, requested RPM, measured RPM and explanation."""
    return self._fan_control.status


@endpoint('razer.device.fan', 'getFanGroups', out_sig='a{say}', required_files=_FAN_SELECT_FILES)
def get_fan_groups(self):
    """Get the model's named fan groups and their firmware IDs."""
    groups = {}
    seen = set()
    with open(self.get_driver_path('fan_groups'), 'r') as driver_file:
        for line in driver_file:
            fields = line.split()
            if len(fields) != 2 or not re.fullmatch(r'[a-z][a-z0-9_]*', fields[0]) or fields[0] in groups:
                raise ValueError('Invalid fan group')
            values = fields[1].split(',')
            if not values or any(not value for value in values):
                raise ValueError('Invalid fan group IDs')
            groups[fields[0]] = [_fan_id(value, seen) for value in values]
    if not groups:
        raise ValueError('No fan groups')
    return groups


@endpoint('razer.device.fan', 'setFanManualFans', in_sig='a{yq}', required_files=_FAN_SELECT_FILES)
def set_fan_manual_fans(self, targets):
    """Set only the selected fan IDs to manual RPM targets."""
    if not isinstance(targets, dict) or not targets:
        raise ValueError('Fan targets must be a nonempty mapping')
    checked = {}
    for fan_id, rpm in targets.items():
        if isinstance(fan_id, bool) or not isinstance(fan_id, int) or not 0 < fan_id <= 255:
            raise ValueError('Invalid fan ID')
        if isinstance(rpm, bool) or not isinstance(rpm, int) or not 0 < rpm <= 25500 or rpm % 100:
            raise ValueError('Invalid fan RPM')
        checked[int(fan_id)] = int(rpm)
    self._fan_control.set_manual_fans(checked)


@endpoint('razer.device.fan', 'setFanAutoFans', in_sig='ay', required_files=_FAN_SELECT_FILES)
def set_fan_auto_fans(self, ids):
    """Return only the selected fan IDs to automatic control."""
    if not isinstance(ids, (list, tuple)) or not ids:
        raise ValueError('Fan IDs must be a nonempty sequence')
    checked = []
    seen = set()
    for fan_id in ids:
        if isinstance(fan_id, bool) or not isinstance(fan_id, int) or not 0 < fan_id <= 255 or fan_id in seen:
            raise ValueError('Invalid or duplicate fan ID')
        checked.append(int(fan_id))
        seen.add(int(fan_id))
    self._fan_control.set_auto_fans(tuple(checked))
