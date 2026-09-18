/* SPDX-License-Identifier: GPL-2.0-or-later */
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include "../../../driver/razerblade_models.h"

#define DRIVER_RAZERCOMMON_H_
#define __HID_RAZER_KBD_H
#define USB_DEVICE_ID_RAZER_BLADE_PRO_EARLY_2020 0x0256
#define HID_REQ_GET_REPORT 1
#define USB_TYPE_CLASS 0x20
#define USB_RECIP_INTERFACE 1
#define USB_DIR_IN 0x80
#define USB_CTRL_GET_TIMEOUT 5000
#define GFP_KERNEL 0
#define BIT(n) (1U << (n))
#define PAGE_SIZE 4096
typedef uint8_t u8;
typedef unsigned long ulong;
typedef unsigned short umode_t;

struct kobject {
    int unused;
};
struct device {
    void *driver_data;
    struct device *parent;
    struct kobject kobj;
};
struct hid_device {
    struct device dev;
};
struct usb_device {
    int unused;
};
struct usb_host_interface {
    struct {
        unsigned char bInterfaceNumber;
    } desc;
};
struct usb_interface {
    struct usb_host_interface *cur_altsetting;
};
struct mutex {
    bool held;
};
struct razer_kbd_device {
    struct hid_device *hdev;
    struct mutex lock;
    unsigned short usb_pid;
    bool blade_controls;
    unsigned char blade_transaction;
    const struct razer_blade_model *blade_model;
};
struct razer_report {
    unsigned char prefix[5], data_size, command_class, command_id, arguments[80], crc, reserved;
};
_Static_assert(sizeof(struct razer_report) == 90, "mock report must match wire size");
_Static_assert(offsetof(struct razer_report, arguments) == 8, "mock argument offset");
struct attribute {
    const char *name;
    umode_t mode;
};
struct device_attribute {
    struct attribute attr;
    ssize_t (*show)(struct device *, struct device_attribute *, char *);
    ssize_t (*store)(struct device *, struct device_attribute *, const char *, size_t);
};
struct attribute_group {
    const char *name;
    struct attribute **attrs;
    umode_t (*is_visible)(struct kobject *, struct attribute *, int);
};
#define DEVICE_ATTR(n, mode, show, store) struct device_attribute dev_attr_##n = {{#n, mode}, show, store}
#define module_param(name, type, perm) _Static_assert((perm) == 0444, "module parameter must be read-only")
#define MODULE_PARM_DESC(name, description) _Static_assert(sizeof(description) > 1, "module parameter must be described")

static struct usb_device usb;
static struct usb_host_interface alternate;
static struct usb_interface interface = {&alternate};
static struct hid_device hid;
static struct razer_kbd_device device;
static struct razer_blade_model custom;

struct request_record {
    u8 command_class, command, profile, fan, performance, manual;
};
static struct {
    u8 performance[5], manual[5], target[5], rpm[5];
    u8 fan_count, fan_ids[4];
    u8 expected_interface, expected_fan_profile, expected_info_profile;
    u8 request[90];
    struct request_record sent[256];
    unsigned int sends, receives, writes, pm_gets, pm_puts, pm_refs, warnings;
    unsigned int auto_writes[5], auto_reads[5], warning_fan, busy;
    unsigned int groups_created, groups_removed;
    int pm_error, send_error, receive_error, group_error;
    unsigned int power_sends, power_receives, power_busy;
    int power_send_error, power_receive_error;
    unsigned int power_writes, power_write_busy;
    int power_write_error, power_readback_error;
    bool power_ignore_write;
    u8 power_write_status, power_readback_xor;
    u8 power_flags, power_status, power_payload_size, power_header_fault;
    bool power_bad_checksum;
    u8 fail_target_fan, ignore_auto_fan, fail_auto_read_fan;
    bool coupled_fans;
    u8 rpm_fault_fan, rpm_status, rpm_payload_size, rpm_bad_echo;
    int rpm_receive_error, target_read_error;
    int limits_receive_error;
    u8 limits[3];
} mock;

static struct usb_device *hid_to_usb_dev(struct hid_device *h)
{
    assert(h == &hid);
    return &usb;
}
static struct usb_interface *to_usb_interface(struct device *d)
{
    return &interface;
}
static void *dev_get_drvdata(struct device *d)
{
    return d->driver_data;
}
static struct device *kobj_to_dev(struct kobject *k)
{
    return (struct device *)((char *)k - offsetof(struct device, kobj));
}
static void mutex_lock(struct mutex *m)
{
    assert(!m->held);
    m->held = true;
}
static void mutex_unlock(struct mutex *m)
{
    assert(m->held);
    m->held = false;
}
#define lockdep_assert_held(m) assert((m)->held)
static void fsleep(ulong us)
{
    assert(us == 5000);
}
static void msleep(unsigned int ms)
{
    assert(ms == 200);
}
static int usb_autopm_get_interface(struct usb_interface *i)
{
    assert(i == &interface && device.lock.held);
    mock.pm_gets++;
    if (mock.pm_error)
        return mock.pm_error;
    mock.pm_refs++;
    return 0;
}
static void usb_autopm_put_interface(struct usb_interface *i)
{
    assert(i == &interface && mock.pm_refs == 1 && device.lock.held);
    mock.pm_refs--;
    mock.pm_puts++;
}
static bool sysfs_streq(const char *a, const char *b)
{
    size_t n = strlen(b);
    return !strncmp(a, b, n) && (a[n] == 0 || (a[n] == '\n' && a[n + 1] == 0));
}
static int kstrtouint(const char *s, unsigned int base, unsigned int *value)
{
    char *end;
    unsigned long parsed;
    if (*s == '-' || !*s)
        return -EINVAL;
    errno = 0;
    parsed = strtoul(s, &end, base);
    if (errno || parsed > UINT_MAX || end == s)
        return -ERANGE;
    if (*end == '\n')
        end++;
    if (*end)
        return -EINVAL;
    *value = parsed;
    return 0;
}
static int scnprintf(char *buf, size_t size, const char *format, ...)
{
    int n;
    va_list args;
    assert(size > 0 && size <= PAGE_SIZE);
    va_start(args, format);
    n = vsnprintf(buf, size, format, args);
    va_end(args);
    assert(n >= 0);
    return (size_t)n < size ? n : (int)size - 1;
}
static int sysfs_emit(char *buf, const char *format, ...)
{
    int n;
    va_list args;
    va_start(args, format);
    n = vsnprintf(buf, PAGE_SIZE, format, args);
    va_end(args);
    assert(n >= 0 && n < PAGE_SIZE);
    return n;
}
static int sysfs_create_group(struct kobject *k, const struct attribute_group *g)
{
    assert(!g->name);
    mock.groups_created++;
    return mock.group_error;
}
static void sysfs_remove_group(struct kobject *k, const struct attribute_group *g)
{
    mock.groups_removed++;
}
static void hid_warn(struct hid_device *h, const char *fmt, ...)
{
    va_list args;
    va_start(args, fmt);
    mock.warning_fan = va_arg(args, unsigned int);
    va_end(args);
    mock.warnings++;
}

static int razer_send_control_msg(struct hid_device *h, const void *data,
                                  unsigned int size, unsigned int index, ulong delay)
{
    const u8 *r = data;
    u8 command_class = r[6], cmd = r[7], fan = r[9];
    unsigned int i;
    assert(device.lock.held && mock.pm_refs == 1);
    assert(size == 90 && index == mock.expected_interface && delay == 5000);
    assert(mock.sends < 256);
    mock.sent[mock.sends++] = (struct request_record) {
        command_class, cmd, r[8], fan, r[10], r[11]
    };
    assert(command_class == 0x0D || (command_class == 0x07 && (cmd == 0x8F || cmd == 0x0F)));
    if (command_class == 0x0D && (cmd == 0x01 || cmd == 0x02 || cmd == 0x81 || cmd == 0x82 || cmd == 0x88))
        assert(r[8] == mock.expected_fan_profile);
    if (command_class == 0x0D && cmd == 0x88) {
        assert(r[5] == 3 && (fan == 1 || fan == 2));
        for (i = 10; i < 88; i++)
            assert(!r[i]);
    }
    if (command_class == 0x0D && cmd == 0x83)
        assert(r[8] == mock.expected_info_profile);
    memcpy(mock.request, data, 90);
    if (cmd < 0x80)
        mock.writes++;
    if (mock.send_error)
        return mock.send_error;
    if (command_class == 0x07) {
        assert(r[5] == 1);
        for (i = cmd == 0x8F ? 8 : 9; i < 88; i++)
            assert(!r[i]);
        mock.power_sends++;
        if (cmd == 0x0F) {
            mock.power_writes++;
            if (!mock.power_ignore_write)
                mock.power_flags = r[8];
            return mock.power_write_error;
        }
        return mock.power_send_error;
    }
    if (cmd == 0x02) {
        assert(fan >= 1 && fan <= 4);
        if (!r[11])
            mock.auto_writes[fan]++;
        for (i = 1; i <= mock.fan_count; i++) {
            u8 affected = mock.coupled_fans ? mock.fan_ids[i - 1] : fan;

            if (r[11] || mock.ignore_auto_fan != affected) {
                mock.performance[affected] = r[10];
                mock.manual[affected] = r[11];
            }
            if (!mock.coupled_fans)
                break;
        }
    } else if (cmd == 0x01) {
        assert(fan >= 1 && fan <= 4);
        if (mock.fail_target_fan == fan)
            return -EPIPE;
        for (i = 1; i <= mock.fan_count; i++) {
            u8 affected = mock.coupled_fans ? mock.fan_ids[i - 1] : fan;

            mock.target[affected] = r[10];
            if (!mock.coupled_fans)
                break;
        }
    }
    return 0;
}

static int usb_control_msg_recv(struct usb_device *u, u8 endpoint, u8 request,
                                u8 type, unsigned int value, unsigned int index,
                                void *data, unsigned int size, int timeout, int gfp)
{
    u8 *r = data, command_class = mock.request[6], cmd = mock.request[7], fan = mock.request[9];
    unsigned int i;
    assert(device.lock.held && mock.pm_refs == 1);
    assert(u == &usb && !endpoint && request == 1 && type == 0xA1);
    assert(value == 0x0300 && index == mock.expected_interface && size == 90);
    mock.receives++;
    if (mock.receive_error)
        return mock.receive_error;
    memcpy(r, mock.request, 90);
    r[0] = 2;
    if (command_class == 0x07) {
        mock.power_receives++;
        if (mock.power_receive_error)
            return mock.power_receive_error;
        if (cmd == 0x8F && mock.power_writes && mock.power_readback_error)
            return mock.power_readback_error;
        if (cmd == 0x0F && mock.power_write_busy) {
            mock.power_write_busy--;
            r[0] = 1;
            return 0;
        }
        if (mock.power_busy) {
            mock.power_busy--;
            r[0] = 1;
            return 0;
        }
        r[0] = cmd == 0x0F ? mock.power_write_status : mock.power_status;
        r[5] = mock.power_payload_size;
        r[8] = mock.power_flags;
        if (cmd == 0x8F && mock.power_writes)
            r[8] ^= mock.power_readback_xor;
        if (mock.power_header_fault)
            r[mock.power_header_fault] ^= 1;
        goto checksum;
    }
    if (mock.busy) {
        mock.busy--;
        r[0] = 1;
        return 0;
    }
    switch (cmd) {
    case 0x80:
        r[8] = mock.fan_count;
        memcpy(r + 9, mock.fan_ids, mock.fan_count);
        break;
    case 0x82:
        assert(fan >= 1 && fan <= 4);
        if (mock.auto_writes[fan]) {
            mock.auto_reads[fan]++;
            if (mock.fail_auto_read_fan == fan)
                return -ETIMEDOUT;
        }
        r[10] = mock.performance[fan];
        r[11] = mock.manual[fan];
        break;
    case 0x81:
        if (mock.target_read_error)
            return mock.target_read_error;
        r[10] = mock.target[fan];
        break;
    case 0x88:
        r[10] = mock.rpm[fan];
        if (mock.rpm_fault_fan == fan) {
            if (mock.rpm_receive_error)
                return mock.rpm_receive_error;
            r[0] = mock.rpm_status;
            r[5] = mock.rpm_payload_size;
            if (mock.rpm_bad_echo)
                r[mock.rpm_bad_echo] ^= 1;
        }
        break;
    case 0x83:
        if (mock.limits_receive_error)
            return mock.limits_receive_error;
        r[14] = mock.limits[0];
        r[16] = mock.limits[1];
        r[18] = mock.limits[2];
        break;
    case 0x01:
    case 0x02:
        break;
    default:
        assert(!"unexpected mocked command");
    }
checksum:
    r[88] = 0;
    for (i = 2; i < 88; i++)
        r[88] ^= r[i];
    if (command_class == 0x07 && mock.power_bad_checksum)
        r[88] ^= 1;
    return 0;
}

#include "../../../driver/razerblade_driver.c"

static void reset(void)
{
    memset(&mock, 0, sizeof(mock));
    memset(&device, 0, sizeof(device));
    memset(&hid, 0, sizeof(hid));
    device.hdev = &hid;
    device.usb_pid = 0x0256;
    custom = *razer_blade_lookup_model(0x0256, 2);
    custom.features |= RAZER_BLADE_FAN_SELECT;
    custom.fan_groups = "cpu_gpu 1,2\nbattery 3,4\n";
    device.blade_model = &custom;
    hid.dev.driver_data = &device;
    alternate.desc.bInterfaceNumber = 2;
    mock.target[1] = mock.target[2] = 29;
    mock.fan_count = 2;
    mock.fan_ids[0] = 1;
    mock.fan_ids[1] = 2;
    mock.rpm[1] = 27;
    mock.rpm[2] = 31;
    mock.rpm_status = 2;
    mock.rpm_payload_size = 3;
    mock.expected_interface = 2;
    mock.expected_fan_profile = 1;
    mock.expected_info_profile = 0;
    mock.power_status = 2;
    mock.power_write_status = 2;
    mock.power_payload_size = 1;
    mock.limits[0] = 23;
    mock.limits[1] = 29;
    mock.limits[2] = 43;
}
static void guarded_model(unsigned short product)
{
    device.usb_pid = product;
    device.blade_model = razer_blade_lookup_model(product, 2);
    assert(device.blade_model);
}
static void balanced(void)
{
    assert(!device.lock.held && !mock.pm_refs);
    assert(mock.pm_gets == mock.pm_puts + (mock.pm_error ? 1U : 0U));
}
static ssize_t fan_store(const char *value)
{
    ssize_t ret = fan_control_store(&hid.dev, NULL, value, strlen(value));
    balanced();
    return ret;
}
static ssize_t fan_select_store(const char *value)
{
    ssize_t ret = fan_control_select_store(&hid.dev, NULL, value, strlen(value));
    balanced();
    return ret;
}
static ssize_t fan_target_store(const char *value)
{
    ssize_t ret = fan_control_targets_store(&hid.dev, NULL, value, strlen(value));
    balanced();
    return ret;
}
static unsigned int sent_count(u8 cmd)
{
    unsigned int i, count = 0;
    for (i = 0; i < mock.sends; i++)
        count += mock.sent[i].command == cmd;
    return count;
}
static void check_recovery(void)
{
    unsigned int i;
    assert(fan_store("3000") == -EPIPE);
    assert(mock.auto_writes[1] == 1 && mock.auto_writes[2] == 1);
    assert(mock.auto_reads[1] == 1 && mock.auto_reads[2] == 1);
    assert(!mock.manual[2]);
    assert(mock.sends == 14);
    assert(mock.sent[4].command == 2 && mock.sent[4].fan == 1 && mock.sent[4].manual == 1);
    assert(mock.sent[5].command == 1 && mock.sent[5].fan == 1);
    assert(mock.sent[8].command == 2 && mock.sent[8].fan == 2 && mock.sent[8].manual == 1);
    assert(mock.sent[9].command == 1 && mock.sent[9].fan == 2);
    for (i = 0; i < 2; i++) {
        assert(mock.sent[10 + 2 * i].command == 2 && mock.sent[10 + 2 * i].fan == i + 1);
        assert(!mock.sent[10 + 2 * i].manual);
        assert(mock.sent[11 + 2 * i].command == 0x82 && mock.sent[11 + 2 * i].fan == i + 1);
    }
}
static int exchange(void)
{
    struct razer_report response;
    const u8 args[12] = {0};
    int ret;
    mutex_lock(&device.lock);
    ret = blade_exchange(&device, 0x83, args, sizeof(args), sizeof(args), &response);
    mutex_unlock(&device.lock);
    balanced();
    return ret;
}

int main(int argc, char **argv)
{
    const char *name;
    unsigned int i;
    assert(argc == 2);
    name = argv[1];
    assert(!experimental_fan_targets);
    reset();
    if (!strcmp(name, "partial-recovery")) {
        mock.fail_target_fan = 2;
        check_recovery();
        assert(!mock.manual[1] && !mock.warnings);
    } else if (!strcmp(name, "recovery-mismatch")) {
        mock.fail_target_fan = 2;
        mock.ignore_auto_fan = 1;
        check_recovery();
        assert(mock.manual[1] && mock.warnings == 1 && mock.warning_fan == 1);
    } else if (!strcmp(name, "recovery-read-failure")) {
        mock.fail_target_fan = 2;
        mock.fail_auto_read_fan = 1;
        check_recovery();
        assert(mock.warnings == 1 && mock.warning_fan == 1);
    } else if (!strcmp(name, "reject-input")) {
        const char *invalid[] = {"garbage", "0", "2350", "25600", "-100"};
        const u8 unsupported[] = {4, 6, 255};
        for (i = 0; i < sizeof(invalid) / sizeof(*invalid); i++) {
            reset();
            assert(fan_store(invalid[i]) == -EINVAL);
            assert(!mock.sends);
        }
        for (i = 0; i < sizeof(unsupported); i++) {
            reset();
            mock.performance[2] = unsupported[i];
            assert(fan_store("3000") == -EOPNOTSUPP);
            assert(!mock.writes);
        }
        reset();
        assert(fan_store("4500") == -ERANGE);
        assert(!mock.writes);
        reset();
        mock.performance[1] = 255;
        assert(fan_store("auto") == -EOPNOTSUPP);
        assert(!mock.writes);
    } else if (!strcmp(name, "mixed-performance")) {
        const unsigned short products[] = {0x0256, 0x029F};
        for (i = 0; i < sizeof(products) / sizeof(*products); i++) {
            reset();
            guarded_model(products[i]);
            mock.performance[1] = 0;
            mock.performance[2] = 6;
            mock.manual[1] = mock.manual[2] = 1;
            assert(fan_store("auto") == -EAGAIN);
            assert(mock.manual[1] && mock.manual[2]);
            assert(mock.performance[1] == 0 && mock.performance[2] == 6);
            assert(!mock.writes && !mock.power_sends && !mock.warnings);
        }
    } else if (!strcmp(name, "dc-auto")) {
        mock.performance[1] = mock.performance[2] = 6;
        mock.manual[1] = mock.manual[2] = 1;
        assert(fan_store("auto\n") == 5);
        assert(!mock.manual[1] && !mock.manual[2]);
        assert(mock.performance[1] == 6 && mock.performance[2] == 6);
        assert(sent_count(0x02) == 2 && !sent_count(0x81) && !sent_count(0x83) && !sent_count(0x01));
    } else if (!strcmp(name, "auto-state")) {
        char output[PAGE_SIZE];
        mock.performance[1] = mock.performance[2] = 6;
        mock.target_read_error = -EOPNOTSUPP;
        assert(fan_state_show(&hid.dev, NULL, output) > 0);
        assert(!strcmp(output, "1 6 auto 0\n2 6 auto 0\n"));
        assert(!sent_count(0x81) && !mock.writes);
        mock.manual[1] = 1;
        assert(fan_state_show(&hid.dev, NULL, output) == -EOPNOTSUPP);
        assert(sent_count(0x81) == 1 && !mock.writes);
    } else if (!strcmp(name, "busy")) {
        mock.busy = 2;
        assert(exchange() == 0);
        assert(mock.sends == 1 && mock.receives == 3);
        reset();
        mock.busy = 3;
        assert(exchange() == -EBUSY);
        assert(mock.sends == 1 && mock.receives == 3);
    } else if (!strcmp(name, "pm-errors")) {
        mock.pm_error = -EHOSTUNREACH;
        assert(exchange() == -EHOSTUNREACH);
        assert(!mock.sends && !mock.receives && !mock.pm_puts);
        reset();
        mock.send_error = -EPIPE;
        assert(exchange() == -EPIPE);
        assert(mock.sends == 1 && !mock.receives && mock.pm_puts == 1);
        reset();
        mock.receive_error = -ETIMEDOUT;
        assert(exchange() == -ETIMEDOUT);
        assert(mock.sends == 1 && mock.receives == 1 && mock.pm_puts == 1);
    } else if (!strcmp(name, "max-fan-clear")) {
        const unsigned short products[] = {0x029F, 0x02B8};
        unsigned int p, flags;
        for (p = 0; p < 2; p++) {
            for (flags = 0; flags < 2; flags++) {
                reset();
                guarded_model(products[p]);
                mock.power_flags = flags ? 0x0D : 0;
                assert(fan_store("3000") == 4);
                assert(mock.power_sends == 1 && mock.power_receives == 1);
                assert(mock.power_flags == (flags ? 0x0D : 0));
                assert(mock.writes == 4 && mock.manual[1] && mock.manual[2]);
                for (i = 0; i < mock.sends && mock.sent[i].command >= 0x80; i++)
                    if (mock.sent[i].command_class == 0x07)
                        break;
                assert(i < mock.sends && mock.sent[i].command_class == 0x07);
                assert(fan_store("auto") == 4);
                assert(mock.power_sends == 2 && mock.power_receives == 2);
                assert(!mock.manual[1] && !mock.manual[2]);
            }
        }
        reset();
        assert(fan_store("3000") == 4);
        assert(!mock.power_sends && !mock.power_receives);
        reset();
        guarded_model(0x02C6);
        assert(fan_store("3000") == 4);
        assert(!mock.power_sends && !mock.power_receives);
    } else if (!strcmp(name, "max-fan-active")) {
        const unsigned short products[] = {0x029F, 0x02B8};
        const char *values[] = {"3000", "auto"};
        unsigned int p, v, flags, clear_index;
        for (p = 0; p < 2; p++) {
            for (v = 0; v < 2; v++) {
                for (flags = 2; flags < 256; flags++) {
                    if (!(flags & 2))
                        continue;
                    reset();
                    guarded_model(products[p]);
                    mock.power_flags = flags;
                    assert(fan_store(values[v]) == 4);
                    assert(mock.power_sends == 3 && mock.power_receives == 3);
                    assert(mock.power_writes == 1 && mock.power_flags == (flags & ~2));
                    assert(mock.writes == (v ? 3U : 5U));
                    clear_index = v ? 4 : 5;
                    assert(mock.sent[clear_index].command_class == 7);
                    assert(mock.sent[clear_index].command == 0x0F);
                    assert(mock.sent[clear_index].profile == (flags & ~2));
                    assert(mock.sent[clear_index + 1].command == 0x8F);
                    assert(mock.sent[clear_index + 2].command == 0x02);
                }
            }
        }
    } else if (!strcmp(name, "max-fan-custom")) {
        const unsigned short products[] = {0x029F, 0x02B8};
        for (i = 0; i < 2; i++) {
            reset();
            guarded_model(products[i]);
            mock.performance[1] = mock.performance[2] = 4;
            mock.power_flags = 0x0F;
            assert(fan_store("auto") == -EBUSY);
            assert(mock.power_sends == 1 && !mock.writes);
            assert(mock.power_flags == 0x0F);
            mock.power_flags = 0x0D;
            assert(fan_store("auto") == 4);
            assert(!mock.manual[1] && !mock.manual[2]);
            assert(mock.performance[1] == 4 && mock.performance[2] == 4);
            assert(!mock.power_writes && mock.power_flags == 0x0D);
        }
    } else if (!strcmp(name, "max-fan-preflight")) {
        const char *invalid[] = {"0", "3001", "2200", "4400", "25600", "no"};
        for (i = 0; i < sizeof(invalid) / sizeof(*invalid); i++) {
            reset();
            guarded_model(0x029F);
            mock.power_flags = 0xFF;
            assert(fan_store(invalid[i]) < 0);
            assert(!mock.writes && !mock.power_sends && mock.power_flags == 0xFF);
        }
        reset();
        guarded_model(0x029F);
        mock.performance[1] = mock.performance[2] = 4;
        mock.power_flags = 0xFF;
        assert(fan_store("3000") == -EOPNOTSUPP);
        assert(!mock.writes && !mock.power_sends && mock.power_flags == 0xFF);
        reset();
        guarded_model(0x029F);
        mock.performance[2] = 5;
        mock.power_flags = 0xFF;
        assert(fan_store("auto") == -EAGAIN);
        assert(!mock.writes && !mock.power_sends && mock.power_flags == 0xFF);
        for (i = 0; i < 4; i++) {
            reset();
            guarded_model(0x029F);
            mock.power_flags = 0xFF;
            switch (i) {
            case 0:
                mock.limits_receive_error = -ETIMEDOUT;
                break;
            case 1:
                mock.limits[0] = 0;
                break;
            case 2:
                mock.limits[0] = 30;
                break;
            case 3:
                mock.limits[2] = 28;
                break;
            }
            assert(fan_store("3000") == (i ? -EPROTO : -ETIMEDOUT));
            assert(!mock.writes && !mock.power_sends && mock.power_flags == 0xFF);
        }
    } else if (!strcmp(name, "max-fan-clear-failure")) {
        const char *values[] = {"3000", "auto"};
        const int errors[] = {-EPIPE, -EIO, -ETIMEDOUT, -EIO, -EIO};
        unsigned int v, fault;
        for (v = 0; v < 2; v++) {
            for (fault = 0; fault < sizeof(errors) / sizeof(*errors); fault++) {
                reset();
                guarded_model(0x02B8);
                mock.power_flags = 0xFF;
                switch (fault) {
                case 0:
                    mock.power_write_error = -EPIPE;
                    break;
                case 1:
                    mock.power_write_status = 3;
                    break;
                case 2:
                    mock.power_readback_error = -ETIMEDOUT;
                    break;
                case 3:
                    mock.power_ignore_write = true;
                    break;
                case 4:
                    mock.power_readback_xor = 1;
                    break;
                }
                assert(fan_store(values[v]) == errors[fault]);
                assert(mock.power_writes == 1 && mock.writes == 1);
                assert(!sent_count(0x02) && !sent_count(0x01) && !mock.warnings);
                assert(mock.power_sends == (fault < 2 ? 2U : 3U));
            }
        }
    } else if (!strcmp(name, "max-fan-errors")) {
        const char *values[] = {"3000", "auto"};
        const int errors[] = {-EPIPE, -ETIMEDOUT, -EIO, -ETIMEDOUT, -EOPNOTSUPP, -EPROTO};
        unsigned int v, fault;
        for (v = 0; v < 2; v++) {
            for (fault = 0; fault < sizeof(errors) / sizeof(*errors); fault++) {
                reset();
                guarded_model(0x029F);
                switch (fault) {
                case 0:
                    mock.power_send_error = -EPIPE;
                    break;
                case 1:
                    mock.power_receive_error = -ETIMEDOUT;
                    break;
                case 2:
                    mock.power_status = 3;
                    break;
                case 3:
                    mock.power_status = 4;
                    break;
                case 4:
                    mock.power_status = 5;
                    break;
                case 5:
                    mock.power_status = 0;
                    break;
                }
                assert(fan_store(values[v]) == errors[fault]);
                assert(mock.power_sends == 1 && mock.power_receives == (fault ? 1U : 0U));
                assert(!mock.writes && !mock.warnings);
            }
        }
    } else if (!strcmp(name, "max-fan-malformed")) {
        const char *values[] = {"3000", "auto"};
        unsigned int v, fault;
        for (v = 0; v < 2; v++) {
            for (fault = 0; fault < 6; fault++) {
                reset();
                guarded_model(0x02B8);
                switch (fault) {
                case 0:
                    mock.power_payload_size = 0;
                    break;
                case 1:
                    mock.power_payload_size = 81;
                    break;
                case 2:
                    mock.power_header_fault = 1;
                    break;
                case 3:
                    mock.power_header_fault = 6;
                    break;
                case 4:
                    mock.power_header_fault = 7;
                    break;
                case 5:
                    mock.power_bad_checksum = true;
                    break;
                }
                assert(fan_store(values[v]) == -EPROTO);
                assert(mock.power_sends == 1 && mock.power_receives == 1);
                assert(!mock.writes && !mock.warnings);
            }
        }
    } else if (!strcmp(name, "max-fan-busy")) {
        guarded_model(0x029F);
        mock.power_busy = 2;
        assert(fan_store("3000") == 4);
        assert(mock.power_sends == 1 && mock.power_receives == 3 && mock.writes == 4);
        reset();
        guarded_model(0x029F);
        mock.power_busy = 3;
        assert(fan_store("3000") == -EBUSY);
        assert(mock.power_sends == 1 && mock.power_receives == 3 && !mock.writes);
        reset();
        guarded_model(0x029F);
        mock.power_flags = 2;
        mock.power_write_busy = 2;
        assert(fan_store("3000") == 4);
        assert(mock.power_writes == 1 && mock.power_sends == 3 && mock.power_receives == 5);
        assert(mock.writes == 5 && !mock.power_flags);
        reset();
        guarded_model(0x029F);
        mock.power_flags = 2;
        mock.power_write_busy = 3;
        assert(fan_store("3000") == -EBUSY);
        assert(mock.power_writes == 1 && mock.power_sends == 2 && mock.power_receives == 4);
        assert(mock.writes == 1 && !sent_count(0x02) && !sent_count(0x01));
    } else if (!strcmp(name, "current-rpm")) {
        char output[PAGE_SIZE];
        guarded_model(0x029F);
        mock.power_flags = 0x02;
        mock.rpm[2] = 0;
        assert(fan_rpm_show(&hid.dev, NULL, output) == 11);
        assert(!strcmp(output, "1 2700\n2 0\n"));
        assert(mock.sends == 3 && sent_count(0x80) == 1 && sent_count(0x88) == 2);
        assert(!mock.writes && !mock.power_sends);
        balanced();
        mock.target[1] = 43;
        mock.target[2] = 23;
        mock.performance[1] = mock.performance[2] = 255;
        mock.manual[1] = mock.manual[2] = 1;
        assert(fan_rpm_show(&hid.dev, NULL, output) == 11);
        assert(!strcmp(output, "1 2700\n2 0\n"));
        assert(mock.sends == 6 && sent_count(0x80) == 2 && sent_count(0x88) == 4);
        assert(!mock.writes && !mock.power_sends);
        balanced();
    } else if (!strcmp(name, "current-rpm-errors")) {
        char output[PAGE_SIZE];
        const int errors[] = {-ETIMEDOUT, -EPROTO, -EPROTO, -EPROTO, -EOPNOTSUPP};
        for (i = 0; i < sizeof(errors) / sizeof(*errors); i++) {
            reset();
            mock.rpm_fault_fan = 2;
            switch (i) {
            case 0:
                mock.rpm_receive_error = -ETIMEDOUT;
                break;
            case 1:
                mock.rpm_bad_echo = 8;
                break;
            case 2:
                mock.rpm_bad_echo = 9;
                break;
            case 3:
                mock.rpm_payload_size = 2;
                break;
            case 4:
                mock.rpm_status = 5;
                break;
            }
            assert(fan_rpm_show(&hid.dev, NULL, output) == errors[i]);
            assert(mock.sends == 3 && sent_count(0x88) == 2 && !mock.writes);
            balanced();
        }
        reset();
        mock.send_error = -EPIPE;
        assert(fan_rpm_show(&hid.dev, NULL, output) == -EPIPE);
        assert(mock.sends == 1 && !sent_count(0x88) && !mock.writes);
        balanced();
    } else if (!strcmp(name, "current-rpm-monitored")) {
        char output[PAGE_SIZE];
        mock.fan_count = 4;
        mock.fan_ids[2] = 3;
        mock.fan_ids[3] = 255;
        assert(fan_rpm_show(&hid.dev, NULL, output) > 0);
        assert(!strcmp(output, "1 2700\n2 3100\n"));
        assert(sent_count(0x88) == 2);
        assert(mock.sent[1].fan == 1 && mock.sent[2].fan == 2);
        balanced();
        reset();
        mock.fan_count = 1;
        assert(fan_rpm_show(&hid.dev, NULL, output) == -ENODATA);
        assert(sent_count(0x80) == 1 && !sent_count(0x88));
        balanced();
        custom = *device.blade_model;
        custom.monitored_fans = BIT(1);
        device.blade_model = &custom;
        assert(fan_rpm_show(&hid.dev, NULL, output) > 0);
        assert(!strcmp(output, "1 2700\n"));
        assert(sent_count(0x88) == 1 && !mock.writes);
        balanced();
        custom.monitored_fans = 0;
        assert(fan_rpm_show(&hid.dev, NULL, output) == -ENODATA);
        custom.monitored_fans = BIT(31);
        assert(fan_rpm_show(&hid.dev, NULL, output) == -ENODATA);
        assert(sent_count(0x88) == 1);
        balanced();
    } else if (!strcmp(name, "descriptor-transport-profiles")) {
        custom = *device.blade_model;
        char output[PAGE_SIZE];
        custom.interface_number = 7;
        custom.fan_profile = 3;
        custom.info_profile = 4;
        device.blade_model = &custom;
        mock.expected_interface = 7;
        mock.expected_fan_profile = 3;
        mock.expected_info_profile = 4;
        assert(fan_store("3000") == 4);
        assert(mock.manual[1] && mock.manual[2]);
        assert(mock.target[1] == 30 && mock.target[2] == 30);
        assert(sent_count(0x83) == 1 && sent_count(0x01) == 2);
        assert(fan_state_show(&hid.dev, NULL, output) > 0);
        assert(!strcmp(output, "1 0 manual 3000\n2 0 manual 3000\n"));
        assert(fan_rpm_show(&hid.dev, NULL, output) > 0);
        assert(!strcmp(output, "1 2700\n2 3100\n"));
        assert(fan_limits_show(&hid.dev, NULL, output) > 0);
        assert(!strcmp(output, "2300 2900 4300\n"));
        balanced();
    } else if (!strcmp(name, "descriptor-mode-masks")) {
        custom = *device.blade_model;
        custom.manual_modes = BIT(2);
        custom.automatic_modes = BIT(2) | BIT(5);
        device.blade_model = &custom;
        assert(fan_store("3000") == -EOPNOTSUPP && !mock.writes);
        mock.performance[1] = mock.performance[2] = 2;
        assert(fan_store("3000") == 4);
        assert(mock.performance[1] == 2 && mock.performance[2] == 2);
        assert(mock.manual[1] && mock.manual[2]);
        mock.performance[1] = mock.performance[2] = 5;
        i = mock.writes;
        assert(fan_store("3000") == -EOPNOTSUPP && mock.writes == i);
        assert(fan_store("auto") == 4);
        assert(mock.performance[1] == 5 && mock.performance[2] == 5);
        assert(!mock.manual[1] && !mock.manual[2]);
    } else if (!strcmp(name, "feature-visibility")) {
        experimental_fan_targets = true;
        custom = *device.blade_model;
        device.blade_model = &custom;
        assert(blade_group.is_visible);
        for (i = 0; blade_group.attrs[i]; i++) {
            struct attribute *attr = blade_group.attrs[i];
            assert(!strncmp(attr->name, "fan_", 4));
            custom.features = 0;
            assert(!blade_group.is_visible(&hid.dev.kobj, attr, i));
            custom.features = RAZER_BLADE_MAX_FAN_OVERRIDE;
            assert(!blade_group.is_visible(&hid.dev.kobj, attr, i));
            custom.features = RAZER_BLADE_FAN_SELECT;
            assert(!blade_group.is_visible(&hid.dev.kobj, attr, i));
            custom.features = RAZER_BLADE_FAN_TARGETS;
            assert(!blade_group.is_visible(&hid.dev.kobj, attr, i));
            custom.features = RAZER_BLADE_FAN_CONTROL;
            assert(blade_group.is_visible(&hid.dev.kobj, attr, i) == (i < 6 ? attr->mode : 0));
            custom.features |= RAZER_BLADE_MAX_FAN_OVERRIDE;
            assert(blade_group.is_visible(&hid.dev.kobj, attr, i) == (i < 6 ? attr->mode : 0));
            custom.features = RAZER_BLADE_FAN_CONTROL | RAZER_BLADE_FAN_SELECT;
            assert(blade_group.is_visible(&hid.dev.kobj, attr, i) == (i < 8 ? attr->mode : 0));
            custom.features = RAZER_BLADE_FAN_CONTROL | RAZER_BLADE_FAN_TARGETS;
            assert(blade_group.is_visible(&hid.dev.kobj, attr, i) == (i < 6 || i >= 8 ? attr->mode : 0));
            custom.features |= RAZER_BLADE_FAN_SELECT;
            assert(blade_group.is_visible(&hid.dev.kobj, attr, i) == attr->mode);
        }
        assert(i == 10);
        assert(!blade_group.name);
        assert(dev_attr_fan_state.attr.mode == 0440 && dev_attr_fan_limits.attr.mode == 0440);
        assert(dev_attr_fan_rpm.attr.mode == 0440 && !dev_attr_fan_rpm.store);
        assert(dev_attr_fan_control.attr.mode == 0220);
        assert(dev_attr_fan_modes.attr.mode == 0440 && !dev_attr_fan_modes.store);
        assert(dev_attr_fan_rpm_monitor.attr.mode == 0440 && !dev_attr_fan_rpm_monitor.store);
        assert(dev_attr_fan_groups.attr.mode == 0440 && !dev_attr_fan_groups.store);
        assert(dev_attr_fan_control_select.attr.mode == 0220 && !dev_attr_fan_control_select.show);
        assert(dev_attr_fan_target_ids.attr.mode == 0440 && !dev_attr_fan_target_ids.store);
        assert(dev_attr_fan_control_targets.attr.mode == 0220 && !dev_attr_fan_control_targets.show);
        assert(!mock.sends && !mock.receives && !mock.pm_gets);
    } else if (!strcmp(name, "model-metadata")) {
        char output[PAGE_SIZE];
        experimental_fan_targets = true;
        assert(fan_modes_show(&hid.dev, NULL, output) > 0);
        assert(!strcmp(output, "81 1\n"));
        assert(fan_rpm_monitor_show(&hid.dev, NULL, output) > 0);
        assert(!strcmp(output, "6\n"));
        assert(fan_target_ids_show(&hid.dev, NULL, output) > 0);
        assert(!strcmp(output, "1,2\n"));
        assert(fan_groups_show(&hid.dev, NULL, output) > 0);
        assert(!strcmp(output, "cpu_gpu 1,2\nbattery 3,4\n"));
        custom = *device.blade_model;
        custom.automatic_modes = BIT(2) | BIT(5);
        custom.manual_modes = BIT(2);
        custom.monitored_fans = BIT(1);
        device.blade_model = &custom;
        assert(fan_modes_show(&hid.dev, NULL, output) > 0);
        assert(!strcmp(output, "36 4\n"));
        assert(fan_rpm_monitor_show(&hid.dev, NULL, output) > 0);
        assert(!strcmp(output, "2\n"));
        assert(!mock.sends && !mock.receives && !mock.pm_gets);
    } else if (!strcmp(name, "experimental-target-gate")) {
        char output[PAGE_SIZE];
        assert(!experimental_fan_targets);
        assert(!blade_group.is_visible(&hid.dev.kobj, &dev_attr_fan_target_ids.attr, 8));
        assert(!blade_group.is_visible(&hid.dev.kobj, &dev_attr_fan_control_targets.attr, 9));
        assert(fan_target_ids_show(&hid.dev, NULL, output) == -EOPNOTSUPP);
        assert(fan_target_store("1:4300") == -EOPNOTSUPP);
        assert(!mock.sends && !mock.receives && !mock.pm_gets && !mock.writes);
        for (i = 0; i < 6; i++)
            assert(blade_group.is_visible(&hid.dev.kobj, blade_group.attrs[i], i) == blade_group.attrs[i]->mode);
        assert(fan_store("2900") == 4);
        assert(mock.manual[1] && mock.manual[2]);
        assert(fan_store("auto") == 4);
        assert(!mock.manual[1] && !mock.manual[2]);
        reset();
        experimental_fan_targets = true;
        assert(blade_group.is_visible(&hid.dev.kobj, &dev_attr_fan_target_ids.attr, 8) == 0440);
        assert(blade_group.is_visible(&hid.dev.kobj, &dev_attr_fan_control_targets.attr, 9) == 0220);
        assert(fan_target_ids_show(&hid.dev, NULL, output) == 4);
        assert(!strcmp(output, "1,2\n"));
        assert(!mock.sends && !mock.receives && !mock.pm_gets && !mock.writes);
        custom.target_fans = 0;
        assert(!blade_group.is_visible(&hid.dev.kobj, &dev_attr_fan_target_ids.attr, 8));
        assert(!blade_group.is_visible(&hid.dev.kobj, &dev_attr_fan_control_targets.attr, 9));
        assert(fan_target_ids_show(&hid.dev, NULL, output) == -EOPNOTSUPP);
        assert(fan_target_store("1:4300") == -EOPNOTSUPP);
        guarded_model(0x029F);
        assert(!blade_group.is_visible(&hid.dev.kobj, &dev_attr_fan_target_ids.attr, 8));
        assert(!blade_group.is_visible(&hid.dev.kobj, &dev_attr_fan_control_targets.attr, 9));
        assert(fan_target_ids_show(&hid.dev, NULL, output) == -EOPNOTSUPP);
        assert(fan_target_store("1:4300") == -EOPNOTSUPP);
        assert(!mock.sends && !mock.receives && !mock.pm_gets && !mock.writes);
    } else if (!strcmp(name, "target-only")) {
        char output[PAGE_SIZE];
        experimental_fan_targets = true;
        mock.fan_count = 4;
        mock.fan_ids[2] = 3;
        mock.fan_ids[3] = 4;
        for (i = 1; i <= 4; i++) {
            mock.manual[i] = 1;
            mock.target[i] = 29;
        }
        assert(fan_target_ids_show(&hid.dev, NULL, output) == 4);
        assert(!strcmp(output, "1,2\n"));
        assert(!mock.sends);
        assert(fan_target_store("1:4300,2:2900\n") == 14);
        assert(mock.target[1] == 43 && mock.target[2] == 29);
        assert(mock.target[3] == 29 && mock.target[4] == 29);
        assert(sent_count(0x01) == 2 && sent_count(0x02) == 0);
        assert(mock.writes == 2 && !mock.warnings);
        for (i = 1; i <= 4; i++)
            assert(mock.manual[i] == 1 && !mock.auto_writes[i]);
    } else if (!strcmp(name, "target-preflight")) {
        const char *invalid[] = {"", "auto", "manual 1:4300", "1:0", "1:4301",
                                 "1:4300,", "1:4300,1:2900", "3:2900", "1:25600",
                                 "1:27900"};
        experimental_fan_targets = true;
        for (i = 0; i < sizeof(invalid) / sizeof(*invalid); i++) {
            reset();
            assert(fan_target_store(invalid[i]) == -EINVAL);
            assert(!mock.sends && !mock.writes);
        }
        reset();
        assert(fan_target_store("1:2200") == -EAGAIN && !mock.writes);
        reset();
        mock.manual[1] = mock.manual[2] = 1;
        assert(fan_target_store("1:2200") == -ERANGE && !mock.writes);
        assert(fan_target_store("2:4400") == -ERANGE && !mock.writes);
        reset();
        mock.manual[1] = 1;
        assert(fan_target_store("1:4300") == -EAGAIN && !mock.writes);
        reset();
        mock.manual[1] = mock.manual[2] = 1;
        mock.performance[2] = 6;
        assert(fan_target_store("1:4300") == -EAGAIN && !mock.writes);
        reset();
        mock.manual[1] = 1;
        mock.fan_count = 1;
        assert(fan_target_store("2:4300") == -ENODEV && !mock.writes);
        reset();
        guarded_model(0x029F);
        assert(fan_target_store("1:4300") == -EOPNOTSUPP && !mock.sends);
    } else if (!strcmp(name, "target-coupled")) {
        experimental_fan_targets = true;
        mock.fan_count = 4;
        mock.fan_ids[2] = 3;
        mock.fan_ids[3] = 4;
        for (i = 1; i <= 4; i++) {
            mock.manual[i] = 1;
            mock.target[i] = 29;
        }
        mock.coupled_fans = true;
        assert(fan_target_store("1:4300") == -EIO);
        assert(sent_count(0x01) == 1 && sent_count(0x02) == 4);
        for (i = 1; i <= 4; i++) {
            assert(!mock.manual[i]);
            assert(mock.auto_writes[i] == 1);
        }
        assert(!mock.warnings);
    } else if (!strcmp(name, "target-error-recovery")) {
        experimental_fan_targets = true;
        mock.fan_count = 4;
        mock.fan_ids[2] = 3;
        mock.fan_ids[3] = 4;
        for (i = 1; i <= 4; i++) {
            mock.manual[i] = 1;
            mock.target[i] = 29;
        }
        mock.fail_target_fan = 2;
        assert(fan_target_store("1:4300,2:2900") == -EPIPE);
        assert(sent_count(0x01) == 2 && sent_count(0x02) == 4);
        for (i = 1; i <= 4; i++)
            assert(!mock.manual[i] && mock.auto_writes[i] == 1);
    } else if (!strcmp(name, "selected-control")) {
        mock.fan_count = 4;
        mock.fan_ids[2] = 3;
        mock.fan_ids[3] = 4;
        mock.manual[3] = mock.manual[4] = 1;
        mock.target[3] = 23;
        mock.target[4] = 43;
        mock.performance[3] = mock.performance[4] = 6;
        assert(fan_select_store("manual 1:2900,2:3000\n") == 21);
        assert(mock.manual[1] && mock.manual[2]);
        assert(mock.target[1] == 29 && mock.target[2] == 30);
        assert(mock.manual[3] && mock.manual[4]);
        assert(mock.target[3] == 23 && mock.target[4] == 43);
        assert(mock.performance[3] == 6 && mock.performance[4] == 6);
        assert(mock.writes == 4);
        assert(fan_select_store("auto 1") == 6);
        assert(!mock.manual[1] && mock.manual[2] && mock.manual[3] && mock.manual[4]);
        assert(mock.writes == 5);
        for (i = 0; i < mock.sends; i++)
            if (mock.sent[i].command == 0x01 || mock.sent[i].command == 0x02)
                assert(mock.sent[i].fan == 1 || mock.sent[i].fan == 2);
    } else if (!strcmp(name, "selected-recovery")) {
        mock.fan_count = 4;
        mock.fan_ids[2] = 3;
        mock.fan_ids[3] = 4;
        mock.manual[3] = mock.manual[4] = 1;
        mock.target[3] = 23;
        mock.target[4] = 43;
        mock.fail_target_fan = 2;
        assert(fan_select_store("manual 1:2900,2:3000") == -EPIPE);
        assert(!mock.manual[1] && !mock.manual[2]);
        assert(mock.auto_writes[1] == 1 && mock.auto_writes[2] == 1);
        assert(!mock.auto_writes[3] && !mock.auto_writes[4]);
        assert(mock.manual[3] && mock.manual[4]);
        assert(mock.target[3] == 23 && mock.target[4] == 43);
        assert(!mock.warnings);
        for (i = 0; i < mock.sends; i++)
            if (mock.sent[i].command == 0x01 || mock.sent[i].command == 0x02)
                assert(mock.sent[i].fan == 1 || mock.sent[i].fan == 2);
    } else if (!strcmp(name, "selected-auxiliary")) {
        mock.fan_count = 4;
        mock.fan_ids[2] = 3;
        mock.fan_ids[3] = 4;
        mock.performance[3] = mock.performance[4] = 6;
        mock.manual[3] = mock.manual[4] = 1;
        mock.target[3] = 23;
        mock.target[4] = 43;
        mock.manual[1] = mock.manual[2] = 1;
        assert(fan_select_store("auto 3,4") == 8);
        assert(!mock.manual[3] && !mock.manual[4]);
        assert(mock.manual[1] && mock.manual[2]);
        mock.performance[3] = mock.performance[4] = 0;
        assert(fan_select_store("manual 3:2900,4:3000") == 20);
        assert(mock.manual[3] && mock.manual[4]);
        assert(mock.target[3] == 29 && mock.target[4] == 30);
        assert(mock.manual[1] && mock.manual[2]);
        for (i = 0; i < mock.sends; i++)
            if (mock.sent[i].command == 0x01 || mock.sent[i].command == 0x02)
                assert(mock.sent[i].fan == 3 || mock.sent[i].fan == 4);
    } else if (!strcmp(name, "selected-preflight")) {
        const char *invalid[] = {"", "auto", "auto ", "auto 0", "auto 1,", "auto 1,1",
                                 "auto 1:2900", "auto 1 2", "auto 1,,2", "auto 256",
                                 "manual", "manual 1", "manual 1:", "manual 1:0",
                                 "manual 1:2901", "manual 1:2900,", "manual 1:2900,1:3000",
                                 "manual 1:2900 2:3000", "manual 1:25600", "auto 1\n\n"};
        const char embedded_nul[] = {'a', 'u', 't', 'o', ' ', '1', '\0', ',', '2'};
        char page_input[PAGE_SIZE + 1];
        for (i = 0; i < sizeof(invalid) / sizeof(*invalid); i++) {
            reset();
            assert(fan_select_store(invalid[i]) == -EINVAL);
            assert(!mock.sends && !mock.writes);
        }
        reset();
        assert(fan_control_select_store(&hid.dev, NULL, embedded_nul, sizeof(embedded_nul)) == -EINVAL);
        memset(page_input, '0', sizeof(page_input));
        memcpy(page_input, "auto ", 5);
        assert(fan_control_select_store(&hid.dev, NULL, page_input, PAGE_SIZE) == -EINVAL);
        assert(fan_control_select_store(&hid.dev, NULL, page_input, sizeof(page_input)) == -EINVAL);
        balanced();
        assert(!mock.sends && !mock.writes);
        reset();
        assert(fan_select_store("manual 3:2900") == -ENODEV && !mock.writes);
        reset();
        assert(fan_select_store("manual 1:2200") == -ERANGE && !mock.writes);
        reset();
        assert(fan_select_store("manual 1:4400") == -ERANGE && !mock.writes);
        reset();
        mock.performance[2] = 6;
        assert(fan_select_store("auto 1,2") == -EAGAIN && !mock.writes);
        reset();
        mock.performance[1] = 4;
        assert(fan_select_store("manual 1:2900") == -EOPNOTSUPP && !mock.writes);
        reset();
        guarded_model(0x029F);
        assert(fan_select_store("auto 1") == -EOPNOTSUPP && !mock.sends);
    } else if (!strcmp(name, "selected-coupled")) {
        mock.fan_count = 4;
        mock.fan_ids[2] = 3;
        mock.fan_ids[3] = 4;
        mock.coupled_fans = true;
        assert(fan_select_store("manual 3:2900") == -EIO);
        for (i = 1; i <= 4; i++) {
            assert(!mock.manual[i]);
            assert(mock.auto_writes[i] == 1);
        }
        assert(!mock.warnings);
    } else if (!strcmp(name, "selected-coupled-targets")) {
        mock.coupled_fans = true;
        assert(fan_select_store("manual 1:2900,2:3000") == -EIO);
        assert(!mock.manual[1] && !mock.manual[2]);
        assert(mock.auto_writes[1] == 1 && mock.auto_writes[2] == 1);
        assert(!mock.warnings);
    } else if (!strcmp(name, "sysfs-gate")) {
        device.usb_pid = 0xFFFF;
        assert(!razer_blade_init(&device) && !device.blade_controls && !mock.groups_created);
        assert(!device.blade_model);
        device.usb_pid = 0x0256;
        alternate.desc.bInterfaceNumber = 1;
        assert(!razer_blade_init(&device) && !device.blade_controls && !mock.groups_created);
        alternate.desc.bInterfaceNumber = 2;
        mock.group_error = -ENOMEM;
        assert(razer_blade_init(&device) == -ENOMEM && !device.blade_controls);
        assert(!device.blade_model);
        razer_blade_remove(&device);
        assert(!mock.groups_removed);
        mock.group_error = 0;
        assert(!razer_blade_init(&device) && device.blade_controls);
        assert(!(device.blade_model->features & RAZER_BLADE_FAN_SELECT));
        assert(!blade_group.is_visible(&hid.dev.kobj, &dev_attr_fan_groups.attr, 6));
        assert(!blade_group.is_visible(&hid.dev.kobj, &dev_attr_fan_control_select.attr, 7));
        assert(device.blade_model->features & RAZER_BLADE_FAN_TARGETS);
        assert(device.blade_model->target_fans == (BIT(1) | BIT(2)));
        assert(!blade_group.is_visible(&hid.dev.kobj, &dev_attr_fan_target_ids.attr, 8));
        assert(!blade_group.is_visible(&hid.dev.kobj, &dev_attr_fan_control_targets.attr, 9));
        experimental_fan_targets = true;
        assert(blade_group.is_visible(&hid.dev.kobj, &dev_attr_fan_target_ids.attr, 8) == 0440);
        assert(blade_group.is_visible(&hid.dev.kobj, &dev_attr_fan_control_targets.attr, 9) == 0220);
        assert(fan_select_store("manual 3:2900") == -EOPNOTSUPP);
        razer_blade_remove(&device);
        assert(mock.groups_removed == 1);
        assert(!mock.sends && !mock.receives && !mock.pm_gets);
    } else {
        fprintf(stderr, "Unknown scenario: %s\n", name);
        return 2;
    }
    return 0;
}
