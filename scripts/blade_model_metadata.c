/* SPDX-License-Identifier: GPL-2.0-or-later */
#include <stdio.h>

#include "../driver/razerblade_models.h"

int main(void)
{
    const struct razer_blade_model *model;
    unsigned int product;

    for (product = 0; product <= 0xFFFF; product++) {
        model = razer_blade_lookup_model(product, 2);
        if (model && (model->features & RAZER_BLADE_FAN_CONTROL))
            printf("%04X %u %u %u\n", product, model->automatic_modes,
                   model->manual_modes, model->monitored_fans);
    }
    return 0;
}
