/* SPDX-License-Identifier: GPL-2.0-or-later */
#include <stdio.h>

#include "../driver/razerblade_models.h"

int main(void)
{
    const struct razer_blade_model *model;
    const unsigned char *group;
    unsigned int product;

    for (product = 0; product <= 0xFFFF; product++) {
        model = razer_blade_lookup_model(product, 2);
        if (!model || !(model->features & RAZER_BLADE_FAN_CONTROL))
            continue;
        if (!!(model->features & RAZER_BLADE_FAN_SELECT) != !!model->fan_groups ||
                (model->fan_groups && !*model->fan_groups)) {
            fprintf(stderr, "Invalid fan group registration for %04X\n", product);
            return 1;
        }
        if (!!(model->features & RAZER_BLADE_FAN_TARGETS) != !!model->target_fans ||
                (model->target_fans & 1U)) {
            fprintf(stderr, "Invalid fan target registration for %04X\n", product);
            return 1;
        }
        printf("%04X %u %u %u %u %u ", product, model->automatic_modes,
               model->manual_modes, model->monitored_fans, model->target_fans,
               !!(model->features & RAZER_BLADE_FAN_SELECT));
        if (!model->fan_groups) {
            putchar('-');
        } else {
            for (group = (const unsigned char *)model->fan_groups; *group; group++)
                printf("%02x", *group);
        }
        putchar('\n');
    }
    return 0;
}
