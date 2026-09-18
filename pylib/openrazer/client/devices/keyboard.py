# SPDX-License-Identifier: GPL-2.0-or-later

"""
Contains functionality specific to keyboard-like devices
"""

from openrazer.client.constants import MACRO_LED_STATIC, MACRO_LED_BLINK
from openrazer.client.devices import RazerDevice as __RazerDevice, BaseDeviceFactory as __BaseDeviceFactory


class RazerKeyboard(__RazerDevice):
    @property
    def fan_state(self) -> list[tuple[int, int, str, int]]:
        """Return each fan's ID, performance mode, auto/manual mode and target RPM.

        Automatic mode reports target RPM as zero; read fan_rpm for current speed.
        """
        if self.has('fan_control'):
            return [(int(fan_id), int(performance), str(mode), int(rpm))
                    for fan_id, performance, mode, rpm in self._dbus_interfaces['fan'].getFanState()]
        else:
            raise NotImplementedError()

    @property
    def fan_rpm(self) -> dict[int, int]:
        """Return firmware-reported current RPM by fan ID."""
        if self.has('fan_control'):
            return {int(fan_id): int(rpm) for fan_id, rpm in self._dbus_interfaces['fan'].getFanRPM().items()}
        else:
            raise NotImplementedError()

    @property
    def fan_limits(self) -> tuple[int, int, int]:
        """Return minimum, default and maximum RPM for the current performance mode."""
        if self.has('fan_control'):
            minimum, default, maximum = self._dbus_interfaces['fan'].getFanLimits()
            return int(minimum), int(default), int(maximum)
        else:
            raise NotImplementedError()

    @property
    def fan_config(self) -> dict[str, tuple[int, ...]]:
        """Return registered automatic/manual performance modes and monitored fan IDs."""
        if self.has('fan_control'):
            automatic, manual, monitored = self._dbus_interfaces['fan'].getFanConfig()
            masks = (automatic, manual, monitored)
            if any(isinstance(mask, bool) or not isinstance(mask, int) or not 0 <= mask <= 0xFFFFFFFF for mask in masks):
                raise ValueError('Invalid fan configuration mask')
            return {name: tuple(bit for bit in range(32) if int(mask) & (1 << bit))
                    for name, mask in zip(('automatic_modes', 'manual_modes', 'monitored_fans'), masks)}
        else:
            raise NotImplementedError()

    @property
    def fan_status(self) -> tuple[str, int, dict[int, int], str]:
        """Return cached phase, requested RPM, last measured RPM and detail.

        Phases include idle, auto, suspended, settling, reached, timeout,
        partially_reached, partially_timeout, accepted, error and cancelled.
        A mixed-target request reports target zero; its per-fan targets remain
        in fan_state. Accepted means no selected fan has registered RPM
        telemetry. Partial phases report only monitored fans' settling result.
        The requested RPM may remain while battery power forces automatic
        control. Timeout does not establish a stalled fan.
        """
        if self.has('fan_control'):
            phase, target, current, reason = self._dbus_interfaces['fan'].getFanStatus()
            return str(phase), int(target), {int(fan_id): int(rpm) for fan_id, rpm in current.items()}, str(reason)
        else:
            raise NotImplementedError()

    def set_fan_auto(self) -> None:
        """Restore firmware-controlled fan speed, preserving the performance mode."""
        if self.has('fan_control'):
            self._dbus_interfaces['fan'].setFanAuto()
        else:
            raise NotImplementedError()

    def set_fan_manual(self, rpm: int) -> None:
        """Set all fans to a fixed RPM in steps of 100, subject to device limits.

        Requires AC power and a current performance mode listed in fan_config's
        manual_modes. The performance mode is preserved and firmware RPM limits
        are checked for every request. Returning confirms acceptance; fan_status
        separately reports whether the monitored fans reached the requested RPM.
        """
        if self.has('fan_control'):
            if isinstance(rpm, bool) or not isinstance(rpm, int):
                raise ValueError('RPM must be an integer')
            if rpm <= 0 or rpm > 25500 or rpm % 100:
                raise ValueError('RPM must be between 100 and 25500 in steps of 100')
            self._dbus_interfaces['fan'].setFanManual(rpm)
        else:
            raise NotImplementedError()

    @property
    def fan_target_ids(self) -> tuple[int, ...]:
        """Return experimentally enabled independent fan RPM target IDs.

        Requires the razerkbd experimental_fan_targets module option and model
        support. This capability is unavailable by default.
        """
        if self.has('fan_target_control'):
            return tuple(int(fan_id) for fan_id in self._dbus_interfaces['fan'].getFanTargetIds())
        else:
            raise NotImplementedError()

    def set_fan_manual_targets(self, targets: dict[int, int]) -> None:
        """Experimentally set selected RPM targets in shared manual mode.

        Requires the razerkbd experimental_fan_targets module option and model
        support. Entering manual mode sets all fans to the firmware default RPM
        before applying selected targets. Auto/manual selection remains global.
        """
        if self.has('fan_target_control'):
            if not isinstance(targets, dict) or not targets:
                raise ValueError('Fan targets must be a nonempty mapping')
            checked = {}
            for fan_id, rpm in targets.items():
                if isinstance(fan_id, bool) or not isinstance(fan_id, int) or not 0 < fan_id <= 255:
                    raise ValueError('Invalid fan ID')
                if isinstance(rpm, bool) or not isinstance(rpm, int) or not 0 < rpm <= 25500 or rpm % 100:
                    raise ValueError('Invalid fan RPM')
                checked[fan_id] = rpm
            self._dbus_interfaces['fan'].setFanManualTargets(checked)
        else:
            raise NotImplementedError()

    @property
    def fan_groups(self) -> dict[str, tuple[int, ...]]:
        """Return the model's named fan groups as firmware ID tuples."""
        if self.has('fan_select_control'):
            return {str(name): tuple(int(fan_id) for fan_id in ids)
                    for name, ids in self._dbus_interfaces['fan'].getFanGroups().items()}
        else:
            raise NotImplementedError()

    def set_fan_manual_fans(self, targets: dict[int, int]) -> None:
        """Set selected fan IDs to manual RPM targets in steps of 100."""
        if self.has('fan_select_control'):
            if not isinstance(targets, dict) or not targets:
                raise ValueError('Fan targets must be a nonempty mapping')
            checked = {}
            for fan_id, rpm in targets.items():
                if isinstance(fan_id, bool) or not isinstance(fan_id, int) or not 0 < fan_id <= 255:
                    raise ValueError('Invalid fan ID')
                if isinstance(rpm, bool) or not isinstance(rpm, int) or not 0 < rpm <= 25500 or rpm % 100:
                    raise ValueError('Invalid fan RPM')
                checked[fan_id] = rpm
            self._dbus_interfaces['fan'].setFanManualFans(checked)
        else:
            raise NotImplementedError()

    def set_fan_auto_fans(self, ids: tuple[int, ...]) -> None:
        """Return selected fan IDs to automatic control."""
        if self.has('fan_select_control'):
            if not isinstance(ids, (list, tuple)) or not ids:
                raise ValueError('Fan IDs must be a nonempty sequence')
            checked = []
            seen = set()
            for fan_id in ids:
                if isinstance(fan_id, bool) or not isinstance(fan_id, int) or not 0 < fan_id <= 255 or fan_id in seen:
                    raise ValueError('Invalid or duplicate fan ID')
                checked.append(fan_id)
                seen.add(fan_id)
            self._dbus_interfaces['fan'].setFanAutoFans(checked)
        else:
            raise NotImplementedError()

    def set_fan_group_manual(self, name: str, rpm: int) -> None:
        """Set one registered fan group to a common manual RPM."""
        if not self.has('fan_select_control'):
            raise NotImplementedError()
        groups = self.fan_groups
        if name not in groups:
            raise ValueError('Unknown fan group')
        if not set(groups[name]) <= {fan_id for fan_id, _, _, _ in self.fan_state}:
            raise ValueError('Fan group IDs are absent from current state')
        self.set_fan_manual_fans({fan_id: rpm for fan_id in groups[name]})

    def set_fan_group_auto(self, name: str) -> None:
        """Return one registered fan group to automatic control."""
        if not self.has('fan_select_control'):
            raise NotImplementedError()
        groups = self.fan_groups
        if name not in groups:
            raise ValueError('Unknown fan group')
        if not set(groups[name]) <= {fan_id for fan_id, _, _, _ in self.fan_state}:
            raise ValueError('Fan group IDs are absent from current state')
        self.set_fan_auto_fans(groups[name])

    @property
    def game_mode_led(self) -> bool:
        """
        Get game mode LED state

        :return: LED state
        :rtype: bool
        """
        if self.has('game_mode_led'):
            return bool(self._dbus_interfaces['game_mode_led'].getGameMode())
        else:
            raise NotImplementedError()

    @game_mode_led.setter
    def game_mode_led(self, value: bool) -> None:
        """
        Set game mode LED state

        :param value: LED State
        :type value: bool
        """
        if self.has('game_mode_led'):
            if value:
                self._dbus_interfaces['game_mode_led'].setGameMode(True)
            else:
                self._dbus_interfaces['game_mode_led'].setGameMode(False)
        else:
            raise NotImplementedError()

    @property
    def macro_mode_led(self) -> bool:
        """
        Get macro mode LED state

        :return: LED state
        :rtype: bool
        """
        if self.has('macro_mode_led'):
            return bool(self._dbus_interfaces['macro_mode_led'].getMacroMode())
        else:
            raise NotImplementedError()

    @macro_mode_led.setter
    def macro_mode_led(self, value: bool) -> None:
        """
        Set macro mode LED state

        :param value: LED State
        :type value: bool
        """
        if self.has('macro_mode_led'):
            if value:
                self._dbus_interfaces['macro_mode_led'].setMacroMode(True)
            else:
                self._dbus_interfaces['macro_mode_led'].setMacroMode(False)
        else:
            raise NotImplementedError()

    @property
    def macro_mode_led_effect(self) -> int:
        """
        Get macro mode LED effect

        Can get either blinking or static
        :return: Effect ID
        :rtype: int
        """
        if self.has('macro_mode_led_effect'):
            return int(self._dbus_interfaces['macro_mode_led'].getMacroEffect())
        else:
            raise NotImplementedError()

    @macro_mode_led_effect.setter
    def macro_mode_led_effect(self, value: int) -> None:
        """
        Set macro mode LED effect

        Can set to either MACRO_LED_STATIC or MACRO_LED_BLINK
        :param value: Effect ID
        :type value: int
        """
        if self.has('macro_mode_led_effect'):
            if value in (MACRO_LED_STATIC, MACRO_LED_BLINK):
                self._dbus_interfaces['macro_mode_led'].setMacroEffect(value)
            else:
                raise ValueError(f"Unexpected MACRO_LED_* value passed {value}")
        else:
            raise NotImplementedError()

    @property
    def keyswitch_optimization(self) -> bool:
        """
        Get keyswitch optimization state

        :return: Keyswitch optimization state
        :rtype: bool
        """
        if self.has('keyswitch_optimization'):
            return bool(self._dbus_interfaces['keyswitch_optimization'].getKeyswitchOptimization())
        else:
            raise NotImplementedError()

    @keyswitch_optimization.setter
    def keyswitch_optimization(self, value: bool) -> None:
        """
        Set keyswitch optimization state

        :param value: Keyswitch optimization state
        :type value: bool
        """
        if self.has('keyswitch_optimization'):
            self._dbus_interfaces['keyswitch_optimization'].setKeyswitchOptimization(value)
        else:
            raise NotImplementedError()

    @property
    def profile_led_red(self) -> bool:
        """
         Get red profile LED state

         :return: Red profile LED state
         :rtype: bool
         """
        if self.has('lighting_profile_led_red'):
            return bool(self._dbus_interfaces['profile_led'].getRedLED())
        else:
            raise NotImplementedError()

    @profile_led_red.setter
    def profile_led_red(self, enable: bool) -> None:
        """
        Set red profile LED state

        :param enable: Status of red profile LED
        :type enable: bool
        """
        if self.has('lighting_profile_led_red'):
            self._dbus_interfaces['profile_led'].setRedLED(enable)
        else:
            raise NotImplementedError()

    @property
    def profile_led_green(self) -> bool:
        """
        Get green profile LED state

        :return: Green profile LED state
        :rtype: bool
        """
        if self.has('lighting_profile_led_green'):
            return bool(self._dbus_interfaces['profile_led'].getGreenLED())
        else:
            raise NotImplementedError()

    @profile_led_green.setter
    def profile_led_green(self, enable: bool) -> None:
        """
        Set green profile LED state

        :param enable: Status of green profile LED
        :type enable: bool
        """
        if self.has('lighting_profile_led_green'):
            self._dbus_interfaces['profile_led'].setGreenLED(enable)
        else:
            raise NotImplementedError()

    @property
    def profile_led_blue(self) -> bool:
        """
        Get blue profile LED state

        :return: Blue profile LED state
        :rtype: bool
        """
        if self.has('lighting_profile_led_blue'):
            return bool(self._dbus_interfaces['profile_led'].getBlueLED())
        else:
            raise NotImplementedError()

    @profile_led_blue.setter
    def profile_led_blue(self, enable: bool) -> None:
        """
        Set blue profile LED state

        :param enable: Status of blue profile LED
        :type enable: bool
        """
        if self.has('lighting_profile_led_blue'):
            self._dbus_interfaces['profile_led'].setBlueLED(enable)
        else:
            raise NotImplementedError()
