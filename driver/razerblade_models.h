/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef DRIVER_RAZERBLADE_MODELS_H_
#define DRIVER_RAZERBLADE_MODELS_H_

#define RAZER_BLADE_FAN_CONTROL (1U << 0)
#define RAZER_BLADE_MAX_FAN_OVERRIDE (1U << 1)

struct razer_blade_model {
    unsigned short product_id;
    unsigned char interface_number;
    unsigned char fan_profile;
    unsigned char info_profile;
    unsigned int features;
    unsigned int manual_modes;
    unsigned int automatic_modes;
    unsigned int monitored_fans;
};

const struct razer_blade_model *razer_blade_lookup_model(unsigned short product_id,
        unsigned char interface_number);
int razer_blade_fan_mode_supported(const struct razer_blade_model *model,
                                   unsigned char performance, int manual);

#endif
