// SPDX-License-Identifier: GPL-2.0-or-later
#include "razerblade_models.h"

#define BALANCED_CUSTOM_DC ((1U << 0) | (1U << 4) | (1U << 6))
#define RPM_GROUP_MODEL(pid, auto_modes, extra_features, groups, targets) { \
        .product_id = (pid), \
        .interface_number = 2, \
        .fan_profile = 1, \
        .info_profile = 0, \
        .features = RAZER_BLADE_FAN_CONTROL | (extra_features), \
        .manual_modes = (1U << 0), \
        .automatic_modes = (auto_modes), \
        .monitored_fans = (1U << 1) | (1U << 2), \
        .target_fans = (targets), \
        .fan_groups = (groups), \
    }
#define RPM_MODEL(pid, auto_modes, extra_features) \
    RPM_GROUP_MODEL(pid, auto_modes, extra_features, 0, 0)
#define RPM_TARGET_MODEL(pid, auto_modes, targets) \
    RPM_GROUP_MODEL(pid, auto_modes, RAZER_BLADE_FAN_TARGETS, 0, targets)

static const struct razer_blade_model blade_models[] = {
    RPM_MODEL(0x0253, BALANCED_CUSTOM_DC, 0), /* Blade 15 Advanced (2020) */
    RPM_TARGET_MODEL(0x0256, BALANCED_CUSTOM_DC, (1U << 1) | (1U << 2)), /* Blade Pro 17 (Early 2020) */
    RPM_MODEL(0x026E, BALANCED_CUSTOM_DC, 0), /* Blade 17 Pro (Early 2021) */
    RPM_MODEL(0x0270, BALANCED_CUSTOM_DC, 0), /* Blade 14 (2021, AMD) */
    RPM_MODEL(0x028B, BALANCED_CUSTOM_DC | (1U << 5), 0), /* Blade 17 (2022) */
    RPM_MODEL(0x029F, 0xF1, RAZER_BLADE_MAX_FAN_OVERRIDE), /* Blade 16 (2023) */
    RPM_MODEL(0x02B8, 0xF3, RAZER_BLADE_MAX_FAN_OVERRIDE), /* Blade 18 (2024) */
    RPM_MODEL(0x02C6, 0xFD, 0), /* Blade 16 (2025) */
};

const struct razer_blade_model *razer_blade_lookup_model(unsigned short product_id,
        unsigned char interface_number)
{
    unsigned int i;

    for (i = 0; i < sizeof(blade_models) / sizeof(blade_models[0]); i++) {
        if (blade_models[i].product_id == product_id &&
            blade_models[i].interface_number == interface_number)
            return &blade_models[i];
    }
    return 0;
}

int razer_blade_fan_mode_supported(const struct razer_blade_model *model,
                                   unsigned char performance, int manual)
{
    unsigned int modes;

    if (!model || performance >= 32)
        return 0;
    modes = manual ? model->manual_modes : model->automatic_modes;
    return !!(modes & (1U << performance));
}
