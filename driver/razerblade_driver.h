/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef RAZERBLADE_DRIVER_H
#define RAZERBLADE_DRIVER_H

struct razer_kbd_device;

int razer_blade_init(struct razer_kbd_device *device);
void razer_blade_remove(struct razer_kbd_device *device);

#endif
