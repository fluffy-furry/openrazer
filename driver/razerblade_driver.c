// SPDX-License-Identifier: GPL-2.0-or-later
#include <linux/delay.h>
#include <linux/sysfs.h>

#include "razercommon.h"
#include "razerkbd_driver.h"
#include "razerblade_driver.h"
#include "razerblade_protocol.h"
#include "razerblade_models.h"

#define BLADE_WAIT_US 5000
#define BLADE_MAX_FANS 79

struct blade_fan_state {
    u8 id;
    u8 performance;
    u8 manual;
    u8 target;
};

static int blade_exchange_class(struct razer_kbd_device *device, u8 command_class, u8 command,
                                const u8 *args, unsigned int size, unsigned int minimum,
                                struct razer_report *response)
{
    struct usb_device *usb_dev = hid_to_usb_dev(device->hdev);
    struct usb_interface *intf = to_usb_interface(device->hdev->dev.parent);
    struct razer_report request;
    int err, attempt;

    lockdep_assert_held(&device->lock);
    err = razer_blade_build_request_class((u8 *)&request, device->blade_transaction,
                                          command_class, command, args, size);
    if (err)
        return err;
    device->blade_transaction = (device->blade_transaction + 1) % 31;

    err = usb_autopm_get_interface(intf);
    if (err)
        return err;
    fsleep(BLADE_WAIT_US);
    err = razer_send_control_msg(device->hdev, &request, sizeof(request),
                                 device->blade_model->interface_number, BLADE_WAIT_US);
    if (err)
        goto out;

    // BUSY retries only GET_REPORT; never replay a setting write.
    for (attempt = 0; attempt < 3; attempt++) {
        memset(response, 0, sizeof(*response));
        err = usb_control_msg_recv(usb_dev, 0, HID_REQ_GET_REPORT,
                                   USB_TYPE_CLASS | USB_RECIP_INTERFACE | USB_DIR_IN,
                                   0x0300, device->blade_model->interface_number, response, sizeof(*response),
                                   USB_CTRL_GET_TIMEOUT, GFP_KERNEL);
        if (err)
            break;
        err = razer_blade_validate_response((u8 *)&request, (u8 *)response,
                                            sizeof(*response), minimum);
        if (err != -EBUSY)
            break;
        fsleep(BLADE_WAIT_US);
    }
out:
    usb_autopm_put_interface(intf);
    return err;
}

static int blade_exchange(struct razer_kbd_device *device, u8 command,
                          const u8 *args, unsigned int size, unsigned int minimum,
                          struct razer_report *response)
{
    return blade_exchange_class(device, 0x0D, command, args, size, minimum, response);
}

static int blade_prepare_fan_control(struct razer_kbd_device *device, u8 performance)
{
    struct razer_report response;
    const u8 args[1] = {0};
    u8 flags;
    int err;

    if (!(device->blade_model->features & RAZER_BLADE_MAX_FAN_OVERRIDE))
        return 0;
    err = blade_exchange_class(device, 0x07, 0x8F, args, sizeof(args), 1, &response);
    if (err)
        return err;
    flags = response.arguments[0];
    if (!(flags & 0x02))
        return 0;
    if (performance == 4)
        return -EBUSY;
    flags &= ~0x02;
    err = blade_exchange_class(device, 0x07, 0x0F, &flags, 1, 1, &response);
    if (err)
        return err;
    err = blade_exchange_class(device, 0x07, 0x8F, args, sizeof(args), 1, &response);
    if (err)
        return err;
    return response.arguments[0] == flags ? 0 : -EIO;
}


static int blade_fans_get(struct razer_kbd_device *device,
                          struct blade_fan_state fans[BLADE_MAX_FANS], unsigned int *count)
{
    struct razer_report response;
    const u8 args[80] = {0};
    unsigned int i, j;
    int err = blade_exchange(device, 0x80, args, sizeof(args), 1, &response);

    if (err)
        return err;
    *count = response.arguments[0];
    if (!*count || *count > BLADE_MAX_FANS || response.data_size < *count + 1)
        return -EPROTO;
    for (i = 0; i < *count; i++) {
        fans[i].id = response.arguments[i + 1];
        if (!fans[i].id)
            return -EPROTO;
        for (j = 0; j < i; j++) {
            if (fans[i].id == fans[j].id)
                return -EPROTO;
        }
    }
    return 0;
}

static int blade_fan_mode_get(struct razer_kbd_device *device, struct blade_fan_state *fan)
{
    struct razer_report response;
    const u8 args[4] = {device->blade_model->fan_profile, fan->id, 0, 0};
    int err = blade_exchange(device, 0x82, args, sizeof(args), 4, &response);

    if (err)
        return err;
    if (response.arguments[0] != device->blade_model->fan_profile || response.arguments[1] != fan->id)
        return -EPROTO;
    fan->performance = response.arguments[2];
    fan->manual = response.arguments[3];
    if (fan->manual > 1)
        return -EOPNOTSUPP;
    return 0;
}

static int blade_fan_state_get(struct razer_kbd_device *device, struct blade_fan_state *fan)
{
    struct razer_report response;
    const u8 args[3] = {device->blade_model->fan_profile, fan->id, 0};
    int err = blade_fan_mode_get(device, fan);

    if (err)
        return err;
    if (!fan->manual) {
        fan->target = 0;
        return 0;
    }
    err = blade_exchange(device, 0x81, args, sizeof(args), 3, &response);
    if (err)
        return err;
    if (response.arguments[0] != device->blade_model->fan_profile || response.arguments[1] != fan->id)
        return -EPROTO;
    fan->target = response.arguments[2];
    return 0;
}

static int blade_fan_rpm_get(struct razer_kbd_device *device, u8 fan_id, unsigned int *rpm)
{
    struct razer_report response;
    const u8 args[3] = {device->blade_model->fan_profile, fan_id, 0};
    int err = blade_exchange(device, 0x88, args, sizeof(args), 3, &response);

    if (err)
        return err;
    if (response.arguments[0] != device->blade_model->fan_profile || response.arguments[1] != fan_id)
        return -EPROTO;
    *rpm = response.arguments[2] * 100;
    return 0;
}

static int blade_limits_get(struct razer_kbd_device *device, u8 limits[3])
{
    struct razer_report response;
    const u8 args[12] = {device->blade_model->info_profile};
    int err = blade_exchange(device, 0x83, args, sizeof(args), 12, &response);

    if (err)
        return err;
    limits[0] = response.arguments[6];
    limits[1] = response.arguments[8];
    limits[2] = response.arguments[10];
    if (!limits[0] || limits[0] > limits[1] || limits[1] > limits[2])
        return -EPROTO;
    return 0;
}

static ssize_t fan_state_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    struct razer_kbd_device *device = dev_get_drvdata(dev);
    struct blade_fan_state fans[BLADE_MAX_FANS];
    unsigned int count, i;
    int err, length = 0;

    mutex_lock(&device->lock);
    err = blade_fans_get(device, fans, &count);
    if (err)
        goto out;
    for (i = 0; i < count; i++) {
        err = blade_fan_state_get(device, &fans[i]);
        if (err)
            goto out;
        length += scnprintf(buf + length, PAGE_SIZE - length, "%u %u %s %u\n", fans[i].id,
                            fans[i].performance, fans[i].manual ? "manual" : "auto",
                            fans[i].target * 100);
    }
out:
    mutex_unlock(&device->lock);
    return err ? err : length;
}

static ssize_t fan_rpm_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    struct razer_kbd_device *device = dev_get_drvdata(dev);
    struct blade_fan_state fans[BLADE_MAX_FANS];
    unsigned int count, i, rpm, available = 0;
    unsigned int monitored = device->blade_model->monitored_fans;
    int err, length = 0;

    mutex_lock(&device->lock);
    err = blade_fans_get(device, fans, &count);
    if (err)
        goto out;
    for (i = 0; i < count; i++) {
        if (fans[i].id < 32)
            available |= 1U << fans[i].id;
    }
    if (!monitored || (available & monitored) != monitored) {
        err = -ENODATA;
        goto out;
    }
    for (i = 0; i < count; i++) {
        if (fans[i].id >= 32 || !(monitored & (1U << fans[i].id)))
            continue;
        err = blade_fan_rpm_get(device, fans[i].id, &rpm);
        if (err)
            goto out;
        length += scnprintf(buf + length, PAGE_SIZE - length, "%u %u\n", fans[i].id, rpm);
    }
out:
    mutex_unlock(&device->lock);
    return err ? err : length;
}

static ssize_t fan_limits_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    struct razer_kbd_device *device = dev_get_drvdata(dev);
    u8 limits[3];
    int err;

    mutex_lock(&device->lock);
    err = blade_limits_get(device, limits);
    mutex_unlock(&device->lock);
    if (err)
        return err;
    return sysfs_emit(buf, "%u %u %u\n", limits[0] * 100, limits[1] * 100, limits[2] * 100);
}

static ssize_t fan_modes_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    struct razer_kbd_device *device = dev_get_drvdata(dev);

    return sysfs_emit(buf, "%u %u\n", device->blade_model->automatic_modes,
                      device->blade_model->manual_modes);
}

static ssize_t fan_rpm_monitor_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    struct razer_kbd_device *device = dev_get_drvdata(dev);

    return sysfs_emit(buf, "%u\n", device->blade_model->monitored_fans);
}

static ssize_t fan_groups_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    struct razer_kbd_device *device = dev_get_drvdata(dev);

    if (!device->blade_model->fan_groups)
        return -ENODATA;
    return sysfs_emit(buf, "%s", device->blade_model->fan_groups);
}

static int blade_fan_mode_set(struct razer_kbd_device *device,
                              const struct blade_fan_state *fan, u8 manual)
{
    struct razer_report response;
    const u8 args[4] = {device->blade_model->fan_profile, fan->id, fan->performance, manual};
    int err = blade_exchange(device, 0x02, args, sizeof(args), 4, &response);

    // Allow the thermal mode change to settle before the next fan command.
    msleep(200);
    return err;
}

static ssize_t fan_control_store(struct device *dev, struct device_attribute *attr,
                                 const char *buf, size_t count)
{
    struct razer_kbd_device *device = dev_get_drvdata(dev);
    struct blade_fan_state fans[BLADE_MAX_FANS], readback;
    struct razer_report response;
    unsigned int fan_count, i, rpm = 0;
    u8 limits[3];
    bool manual = !sysfs_streq(buf, "auto");
    int err, recovery;

    if (manual && (kstrtouint(buf, 10, &rpm) || !rpm || rpm % 100 || rpm > 25500))
        return -EINVAL;
    mutex_lock(&device->lock);
    err = blade_fans_get(device, fans, &fan_count);
    if (err)
        goto out;
    for (i = 0; i < fan_count; i++) {
        err = blade_fan_mode_get(device, &fans[i]);
        if (err)
            goto out;
        if (!razer_blade_fan_mode_supported(device->blade_model, fans[i].performance, manual)) {
            err = -EOPNOTSUPP;
            goto out;
        }
        // Mixed modes can indicate a performance transition in progress.
        if (i && fans[i].performance != fans[0].performance) {
            err = -EAGAIN;
            goto out;
        }
    }
    if (manual) {
        err = blade_limits_get(device, limits);
        if (err)
            goto out;
        if (rpm < limits[0] * 100 || rpm > limits[2] * 100) {
            err = -ERANGE;
            goto out;
        }
    }
    err = blade_prepare_fan_control(device, fans[0].performance);
    if (err)
        goto out;
    for (i = 0; i < fan_count; i++) {
        const u8 args[3] = {device->blade_model->fan_profile, fans[i].id, rpm / 100};

        err = blade_fan_mode_set(device, &fans[i], manual);
        if (err)
            goto recover_auto;
        if (manual) {
            err = blade_exchange(device, 0x01, args, sizeof(args), 3, &response);
            if (err)
                goto recover_auto;
        }
        readback.id = fans[i].id;
        err = manual ? blade_fan_state_get(device, &readback) : blade_fan_mode_get(device, &readback);
        if (!err && (readback.performance != fans[i].performance ||
                     readback.manual != manual || (manual && readback.target != rpm / 100)))
            err = -EIO;
        if (err)
            goto recover_auto;
    }
    goto out;

recover_auto:
    // Recover every fan: an errored setter may still have reached firmware.
    for (i = 0; i < fan_count; i++) {
        recovery = blade_fan_mode_set(device, &fans[i], 0);
        if (!recovery) {
            readback.id = fans[i].id;
            recovery = blade_fan_mode_get(device, &readback);
            if (!recovery && (readback.manual || readback.performance != fans[i].performance))
                recovery = -EIO;
        }
        if (recovery)
            hid_warn(device->hdev, "Could not confirm automatic control of fan %u: %d\n",
                     fans[i].id, recovery);
    }
out:
    mutex_unlock(&device->lock);
    return err ? err : count;
}

static int blade_parse_decimal(const char **cursor, const char *end, unsigned int *value)
{
    unsigned int number = 0, digit;

    if (*cursor == end || **cursor < '0' || **cursor > '9')
        return -EINVAL;
    do {
        digit = **cursor - '0';
        if (number > (25500 - digit) / 10)
            return -EINVAL;
        number = number * 10 + digit;
        (*cursor)++;
    } while (*cursor < end && **cursor >= '0' && **cursor <= '9');
    *value = number;
    return 0;
}

static ssize_t fan_control_select_store(struct device *dev, struct device_attribute *attr,
                                        const char *buf, size_t count)
{
    struct razer_kbd_device *device = dev_get_drvdata(dev);
    struct blade_fan_state available[BLADE_MAX_FANS], selected[BLADE_MAX_FANS], readback;
    struct razer_report response;
    unsigned int fan_count, selected_count = 0, i, j, id, value;
    const char *cursor, *end;
    size_t length = count;
    u8 targets[BLADE_MAX_FANS];
    u8 limits[3];
    bool manual;
    int err, recovery;

    if ((device->blade_model->features & (RAZER_BLADE_FAN_CONTROL | RAZER_BLADE_FAN_SELECT)) !=
        (RAZER_BLADE_FAN_CONTROL | RAZER_BLADE_FAN_SELECT))
        return -EOPNOTSUPP;
    if (!count || count > PAGE_SIZE || memchr(buf, '\0', count))
        return -EINVAL;
    if (buf[length - 1] == '\n')
        length--;
    if (length > 7 && !memcmp(buf, "manual ", 7)) {
        manual = true;
        cursor = buf + 7;
    } else if (length > 5 && !memcmp(buf, "auto ", 5)) {
        manual = false;
        cursor = buf + 5;
    } else {
        return -EINVAL;
    }
    end = buf + length;
    while (cursor < end) {
        if (selected_count == BLADE_MAX_FANS)
            return -E2BIG;
        err = blade_parse_decimal(&cursor, end, &id);
        if (err || !id || id > 255)
            return -EINVAL;
        value = 0;
        if (manual) {
            if (cursor == end || *cursor++ != ':')
                return -EINVAL;
            err = blade_parse_decimal(&cursor, end, &value);
            if (err || !value || value % 100)
                return -EINVAL;
        }
        if (cursor < end && *cursor != ',')
            return -EINVAL;
        for (i = 0; i < selected_count; i++) {
            if (selected[i].id == id)
                return -EINVAL;
        }
        selected[selected_count].id = id;
        targets[selected_count++] = value / 100;
        if (cursor == end)
            break;
        cursor++;
        if (cursor == end)
            return -EINVAL;
    }

    mutex_lock(&device->lock);
    err = blade_fans_get(device, available, &fan_count);
    if (err)
        goto out;
    for (i = 0; i < selected_count; i++) {
        for (j = 0; j < fan_count; j++) {
            if (available[j].id == selected[i].id)
                break;
        }
        if (j == fan_count) {
            err = -ENODEV;
            goto out;
        }
        err = blade_fan_mode_get(device, &selected[i]);
        if (err)
            goto out;
        if (!razer_blade_fan_mode_supported(device->blade_model, selected[i].performance, manual)) {
            err = -EOPNOTSUPP;
            goto out;
        }
        if (i && selected[i].performance != selected[0].performance) {
            err = -EAGAIN;
            goto out;
        }
    }
    if (manual) {
        err = blade_limits_get(device, limits);
        if (err)
            goto out;
        for (i = 0; i < selected_count; i++) {
            if (targets[i] < limits[0] || targets[i] > limits[2]) {
                err = -ERANGE;
                goto out;
            }
        }
    }
    err = blade_prepare_fan_control(device, selected[0].performance);
    if (err)
        goto out;
    for (i = 0; i < selected_count; i++) {
        const u8 args[3] = {device->blade_model->fan_profile, selected[i].id, targets[i]};

        err = blade_fan_mode_set(device, &selected[i], manual);
        if (err)
            goto recover_auto;
        if (manual) {
            err = blade_exchange(device, 0x01, args, sizeof(args), 3, &response);
            if (err)
                goto recover_auto;
        }
        readback.id = selected[i].id;
        err = manual ? blade_fan_state_get(device, &readback) : blade_fan_mode_get(device, &readback);
        if (!err && (readback.performance != selected[i].performance ||
                     readback.manual != manual || (manual && readback.target != targets[i])))
            err = -EIO;
        if (err)
            goto recover_auto;
    }
    goto out;

recover_auto:
    for (i = 0; i < selected_count; i++) {
        recovery = blade_fan_mode_set(device, &selected[i], 0);
        if (!recovery) {
            readback.id = selected[i].id;
            recovery = blade_fan_mode_get(device, &readback);
            if (!recovery && (readback.manual || readback.performance != selected[i].performance))
                recovery = -EIO;
        }
        if (recovery)
            hid_warn(device->hdev, "Could not confirm automatic control of fan %u: %d\n",
                     selected[i].id, recovery);
    }
out:
    mutex_unlock(&device->lock);
    return err ? err : count;
}

static DEVICE_ATTR(fan_state,   0440, fan_state_show,  NULL);
static DEVICE_ATTR(fan_rpm,     0440, fan_rpm_show,    NULL);
static DEVICE_ATTR(fan_limits,  0440, fan_limits_show, NULL);
static DEVICE_ATTR(fan_control, 0220, NULL,           fan_control_store);
static DEVICE_ATTR(fan_modes, 0440, fan_modes_show, NULL);
static DEVICE_ATTR(fan_rpm_monitor, 0440, fan_rpm_monitor_show, NULL);
static DEVICE_ATTR(fan_groups, 0440, fan_groups_show, NULL);
static DEVICE_ATTR(fan_control_select, 0220, NULL, fan_control_select_store);

static struct attribute *blade_attributes[] = {
    &dev_attr_fan_state.attr,
    &dev_attr_fan_rpm.attr,
    &dev_attr_fan_limits.attr,
    &dev_attr_fan_control.attr,
    &dev_attr_fan_modes.attr,
    &dev_attr_fan_rpm_monitor.attr,
    &dev_attr_fan_groups.attr,
    &dev_attr_fan_control_select.attr,
    NULL
};

static umode_t blade_attribute_visible(struct kobject *kobj, struct attribute *attr, int index)
{
    struct razer_kbd_device *device = dev_get_drvdata(kobj_to_dev(kobj));
    if (attr == &dev_attr_fan_groups.attr || attr == &dev_attr_fan_control_select.attr)
        return (device->blade_model->features & (RAZER_BLADE_FAN_CONTROL | RAZER_BLADE_FAN_SELECT)) ==
               (RAZER_BLADE_FAN_CONTROL | RAZER_BLADE_FAN_SELECT) ? attr->mode : 0;
    return device->blade_model->features & RAZER_BLADE_FAN_CONTROL ? attr->mode : 0;
}

static const struct attribute_group blade_group = {
    .attrs = blade_attributes,
    .is_visible = blade_attribute_visible,
};

int razer_blade_init(struct razer_kbd_device *device)
{
    struct usb_interface *intf = to_usb_interface(device->hdev->dev.parent);
    int err;

    device->blade_model = razer_blade_lookup_model(device->usb_pid,
                          intf->cur_altsetting->desc.bInterfaceNumber);
    if (!device->blade_model)
        return 0;
    err = sysfs_create_group(&device->hdev->dev.kobj, &blade_group);
    if (!err)
        device->blade_controls = true;
    else
        device->blade_model = NULL;
    return err;
}

void razer_blade_remove(struct razer_kbd_device *device)
{
    if (device->blade_controls)
        sysfs_remove_group(&device->hdev->dev.kobj, &blade_group);
}
