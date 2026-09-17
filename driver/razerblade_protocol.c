// SPDX-License-Identifier: GPL-2.0-or-later

#ifdef __KERNEL__
#include <linux/errno.h>
#include <linux/string.h>
#else
#include <errno.h>
#include <string.h>
#endif

#include "razerblade_protocol.h"

static unsigned char razer_blade_checksum(const unsigned char *report)
{
    unsigned char checksum = 0;
    unsigned int i;

    for (i = 2; i < 88; i++)
        checksum ^= report[i];

    return checksum;
}

int razer_blade_build_request_class(unsigned char *report,
                                    unsigned char transaction_id,
                                    unsigned char command_class,
                                    unsigned char command_id,
                                    const unsigned char *args,
                                    unsigned int data_size)
{
    if (!report || transaction_id > 30 ||
        data_size > RAZER_BLADE_MAX_PAYLOAD_SIZE ||
        (!args && data_size))
        return -EINVAL;

    memset(report, 0, RAZER_BLADE_REPORT_SIZE);
    report[1] = transaction_id;
    report[5] = data_size;
    report[6] = command_class;
    report[7] = command_id;
    if (data_size)
        memcpy(report + 8, args, data_size);
    report[88] = razer_blade_checksum(report);

    return 0;
}

int razer_blade_build_request(unsigned char *report,
                              unsigned char transaction_id,
                              unsigned char command_id,
                              const unsigned char *args,
                              unsigned int data_size)
{
    return razer_blade_build_request_class(report, transaction_id, 0x0D,
                                           command_id, args, data_size);
}

int razer_blade_validate_response(const unsigned char *request,
                                  const unsigned char *response,
                                  unsigned int response_length,
                                  unsigned int minimum_payload)
{
    if (!request || !response || minimum_payload > RAZER_BLADE_MAX_PAYLOAD_SIZE)
        return -EINVAL;

    if (response_length != RAZER_BLADE_REPORT_SIZE ||
        response[5] > RAZER_BLADE_MAX_PAYLOAD_SIZE)
        return -EPROTO;

    // Thermal replies populate bytes 2-3 independently of the request.
    if (response[1] != request[1] ||
        response[4] != request[4] ||
        response[6] != request[6] || response[7] != request[7] ||
        response[88] != razer_blade_checksum(response))
        return -EPROTO;

    switch (response[0]) {
    case 1:
        return -EBUSY;
    case 2:
        if (response[5] < minimum_payload)
            return -EPROTO;
        return 0;
    case 3:
        return -EIO;
    case 4:
        return -ETIMEDOUT;
    case 5:
        return -EOPNOTSUPP;
    default:
        return -EPROTO;
    }
}
