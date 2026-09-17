#!/usr/bin/env python3

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class BladeDriverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("gcc")
        if not compiler:
            raise RuntimeError("gcc is required for the offline Blade driver harness")
        cls.directory = tempfile.TemporaryDirectory(prefix="openrazer-blade-driver-")
        cls.addClassCleanup(cls.directory.cleanup)
        temporary = Path(cls.directory.name)
        # The C harness supplies these kernel definitions.
        (temporary / "linux").mkdir()
        for name in ("delay.h", "sysfs.h"):
            (temporary / "linux" / name).write_text("/* Offline harness shim. */\n")
        root = Path(__file__).resolve().parents[2]
        cls.executable = temporary / "blade-driver-tests"
        subprocess.run([
            compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
            "-Wno-unused-parameter", "-Wno-sign-compare", "-I", str(temporary),
            str(root / "scripts/ci/fixtures/blade-driver-harness.c"),
            str(root / "driver/razerblade_protocol.c"),
            str(root / "driver/razerblade_models.c"),
            "-o", str(cls.executable),
        ], check=True)

    def scenario(self, name):
        result = subprocess.run([str(self.executable), name], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_partial_manual_failure_restores_all_fans_and_keeps_original_errno(self):
        self.scenario("partial-recovery")

    def test_automatic_recovery_readback_mismatch_warns_and_continues(self):
        self.scenario("recovery-mismatch")

    def test_recovery_read_failure_warns_and_continues(self):
        self.scenario("recovery-read-failure")

    def test_invalid_rpm_unsupported_modes_and_limits_do_not_write(self):
        self.scenario("reject-input")

    def test_dc_automatic_control_does_not_require_target_read(self):
        self.scenario("dc-auto")

    def test_automatic_state_skips_target_but_manual_state_requires_it(self):
        self.scenario("auto-state")

    def test_mixed_supported_performance_modes_block_all_setting_writes(self):
        self.scenario("mixed-performance")

    def test_busy_polls_only_get_and_is_bounded(self):
        self.scenario("busy")

    def test_runtime_pm_failure_and_usb_failures_balance_references(self):
        self.scenario("pm-errors")

    def test_max_fan_clear_guard_precedes_writes_and_preserves_other_power_bits(self):
        self.scenario("max-fan-clear")

    def test_active_max_fan_clears_only_override_bit_before_mode_and_target(self):
        self.scenario("max-fan-active")

    def test_custom_maximum_override_requires_explicit_policy(self):
        self.scenario("max-fan-custom")

    def test_invalid_inputs_limits_and_modes_never_mutate_maximum_override(self):
        self.scenario("max-fan-preflight")

    def test_maximum_override_failed_write_or_readback_blocks_fan_writes(self):
        self.scenario("max-fan-clear-failure")

    def test_max_fan_guard_transport_and_status_errors_prevent_setting_writes(self):
        self.scenario("max-fan-errors")

    def test_max_fan_guard_malformed_replies_prevent_setting_writes(self):
        self.scenario("max-fan-malformed")

    def test_max_fan_guard_busy_retries_only_reads_and_remains_bounded(self):
        self.scenario("max-fan-busy")

    def test_descriptor_controls_transport_and_both_profile_selectors(self):
        self.scenario("descriptor-transport-profiles")

    def test_current_rpm_is_read_only_and_independent_of_targets_and_modes(self):
        self.scenario("current-rpm")

    def test_current_rpm_propagates_query_errors_and_rejects_bad_echoes(self):
        self.scenario("current-rpm-errors")

    def test_rpm_queries_only_registered_fans_and_requires_their_presence(self):
        self.scenario("current-rpm-monitored")

    def test_descriptor_mode_masks_replace_device_specific_hardcoding(self):
        self.scenario("descriptor-mode-masks")

    def test_feature_flags_hide_unsupported_attributes_without_io(self):
        self.scenario("feature-visibility")

    def test_model_policy_metadata_is_read_only_and_requires_no_io(self):
        self.scenario("model-metadata")

    def test_sysfs_group_is_limited_to_supported_product_and_interface(self):
        self.scenario("sysfs-gate")


if __name__ == "__main__":
    unittest.main()
