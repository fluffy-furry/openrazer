/* SPDX-License-Identifier: GPL-2.0-or-later */

#ifndef DRIVER_RAZERBLADE_PROTOCOL_H_
#define DRIVER_RAZERBLADE_PROTOCOL_H_

#define RAZER_BLADE_REPORT_SIZE 90
#define RAZER_BLADE_MAX_PAYLOAD_SIZE 80

// report is 90 bytes without the HID report ID; args must not overlap it.
int razer_blade_build_request(unsigned char *report,
                              unsigned char transaction_id,
                              unsigned char command_id,
                              const unsigned char *args,
                              unsigned int data_size);

int razer_blade_build_request_class(unsigned char *report,
                                    unsigned char transaction_id,
                                    unsigned char command_class,
                                    unsigned char command_id,
                                    const unsigned char *args,
                                    unsigned int data_size);

// minimum_payload applies only to successful replies.
int razer_blade_validate_response(const unsigned char *request,
                                  const unsigned char *response,
                                  unsigned int response_length,
                                  unsigned int minimum_payload);

#endif
