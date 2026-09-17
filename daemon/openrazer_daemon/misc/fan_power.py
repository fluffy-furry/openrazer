# SPDX-License-Identifier: GPL-2.0-or-later

"""System power and sleep notifications for fan controllers."""

import os
from pathlib import Path

import dbus
from gi.repository import GLib


class FanPowerMonitor:
    """Observe system power and allow automatic fan restoration before sleep."""

    _LOGIN_NAME = 'org.freedesktop.login1'
    _LOGIN_PATH = '/org/freedesktop/login1'
    _LOGIN_INTERFACE = 'org.freedesktop.login1.Manager'
    _EXTERNAL_TYPES = frozenset(('Mains', 'USB', 'USB_DCP', 'USB_CDP', 'USB_ACA',
                                 'USB_C', 'USB_PD', 'USB_PD_DRP', 'Wireless',
                                 'BrickID', 'USB_FLOAT', 'Apple_Brick_ID'))

    def __init__(self, logger, supply_root='/sys/class/power_supply', testing=False):
        self._logger = logger
        self._supply_root = Path(supply_root)
        self._testing = testing
        self._controllers = []
        self._source = None
        self._bus = None
        self._manager = None
        self._signal = None
        self._inhibitor = None
        self._sleeping = False
        self._sleep_events = 0
        self._closed = False
        self.sleep_supported = False

    @staticmethod
    def _read(path, default=None):
        try:
            return path.read_text(encoding='ascii').strip()
        except FileNotFoundError:
            if default is not None:
                return default
            raise

    @property
    def power(self):
        """Return a fresh system power classification, or unknown on errors."""
        if self._closed:
            return 'unknown'
        try:
            online = False
            battery = False
            for supply in self._supply_root.iterdir():
                scope = self._read(supply / 'scope', 'System')
                if scope == 'Device':
                    continue
                if scope not in ('System', 'Unknown'):
                    raise ValueError('Invalid power supply scope')
                kind = self._read(supply / 'type')
                if kind == 'Battery':
                    present = self._read(supply / 'present', '1')
                    if present not in ('0', '1'):
                        raise ValueError('Invalid battery presence')
                    battery = battery or present == '1'
                elif kind in self._EXTERNAL_TYPES:
                    state = self._read(supply / 'online')
                    if state not in ('0', '1'):
                        raise ValueError('Invalid power supply online state')
                    online = online or state == '1'
                else:
                    raise ValueError('Unrecognized system power supply type')
            if self._closed:
                return 'unknown'
            if online:
                return 'ac'
            return 'battery' if battery else 'unknown'
        except (OSError, UnicodeError, ValueError) as error:
            self._logger.debug('Could not determine fan power source: %s', error)
            return 'unknown'

    def _notify(self, controller, method, *args):
        try:
            getattr(controller, method)(*args)
        except Exception as error:
            self._logger.warning('Fan controller %s failed: %s', method, error)

    def register(self, controller):
        """Register a controller without changing its requested fan setting."""
        if self._closed:
            raise RuntimeError('Fan power monitor is closed')
        if controller in self._controllers:
            return
        self._controllers.append(controller)
        if len(self._controllers) == 1:
            self._source = GLib.timeout_add_seconds(1, self._poll)
            if not self._testing:
                self._connect_logind()
        if self._closed:
            self._stop_monitoring()
            raise RuntimeError('Fan power monitor is closed')
        if controller not in self._controllers:
            raise RuntimeError('Fan controller registration was cancelled')
        if self._sleeping:
            self._notify(controller, 'prepare_for_sleep', True)
        self._notify(controller, 'update_power', self.power)

    def unregister(self, controller):
        """Stop notifying a controller and release unused monitoring resources."""
        if controller in self._controllers:
            self._controllers.remove(controller)
        if not self._controllers:
            self._stop_monitoring()

    def _poll(self):
        if self._closed or not self._controllers:
            self._source = None
            return False
        power = self.power
        for controller in tuple(self._controllers):
            self._notify(controller, 'update_power', power)
            self._notify(controller, 'check_status')
        return True

    def _connect_logind(self):
        try:
            self._bus = dbus.SystemBus()
            self._signal = self._bus.add_signal_receiver(self.prepare_for_sleep,
                                                         signal_name='PrepareForSleep',
                                                         dbus_interface=self._LOGIN_INTERFACE,
                                                         bus_name=self._LOGIN_NAME,
                                                         path=self._LOGIN_PATH)
            proxy = self._bus.get_object(self._LOGIN_NAME, self._LOGIN_PATH)
            self._manager = dbus.Interface(proxy, self._LOGIN_INTERFACE)
            self._sleeping = True
            sleep_events = self._sleep_events
            sleeping = proxy.Get(self._LOGIN_INTERFACE, 'PreparingForSleep', dbus_interface='org.freedesktop.DBus.Properties')
            if not isinstance(sleeping, (bool, dbus.Boolean)):
                raise ValueError('Invalid pending sleep state')
            if self._sleep_events == sleep_events:
                self._sleeping = bool(sleeping)
            self._acquire_inhibitor()
        except Exception as error:
            self.sleep_supported = False
            self._logger.warning('Could not monitor fan sleep transitions: %s', error)
        finally:
            if self._closed:
                self._stop_monitoring()

    def _acquire_inhibitor(self):
        if self._manager is None or self._inhibitor is not None or self._sleeping or not self._controllers:
            return
        try:
            descriptor = self._manager.Inhibit('sleep', 'OpenRazer', 'Restore automatic fan control', 'delay')
            descriptor = descriptor.take()
            if isinstance(descriptor, bool) or not isinstance(descriptor, int) or descriptor < 0:
                raise ValueError('Invalid sleep inhibitor descriptor')
            if self._closed or self._sleeping or not self._controllers:
                os.close(descriptor)
                return
            self._inhibitor = descriptor
            self.sleep_supported = True
        except Exception as error:
            self.sleep_supported = False
            self._logger.warning('Could not delay sleep for automatic fan restoration: %s', error)

    def _release_inhibitor(self):
        descriptor, self._inhibitor = self._inhibitor, None
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as error:
                self._logger.warning('Could not close fan sleep inhibitor: %s', error)

    def prepare_for_sleep(self, sleeping):
        """Restore fans before releasing the sleep delay; notify on wake."""
        if self._closed:
            return
        self._sleep_events += 1
        self._sleeping = bool(sleeping)
        try:
            for controller in tuple(self._controllers):
                self._notify(controller, 'prepare_for_sleep', self._sleeping)
        finally:
            if self._sleeping:
                self._release_inhibitor()
            else:
                self._acquire_inhibitor()

    def _stop_monitoring(self):
        if self._source is not None:
            GLib.source_remove(self._source)
            self._source = None
        if self._signal is not None:
            try:
                self._signal.remove()
            except Exception as error:
                self._logger.warning('Could not remove fan sleep notification: %s', error)
            self._signal = None
        self._release_inhibitor()
        self._manager = None
        self._bus = None
        self.sleep_supported = False
        self._sleeping = False

    def close(self):
        """Release timers, the sleep subscription and the inhibitor descriptor."""
        self._closed = True
        self._controllers.clear()
        self._stop_monitoring()
