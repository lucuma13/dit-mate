"""Test suite for lifsaver."""

import plistlib
import subprocess
import sys
import tomllib
from typing import ClassVar
from unittest.mock import MagicMock, call, patch

import pytest

from dit_mate import lifsaver

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def mask_ci_environment(monkeypatch):
    """Isolate tests from the GitHub Actions CI environment flag, so that they
    can successfully be no-op on non-macOS platforms and CI still passes a smoke
    test."""
    monkeypatch.delenv("CI", raising=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _completed(returncode=0, stdout="", stderr=""):
    """
    Build a fake CompletedProcess.

    stdout is kept as str because every caller in lifsaver.py that reads
    .stdout as text passes text=True to subprocess.run (get_active_mounts,
    _find_mount_point).  get_disk_data / get_partition_fs_type use plistlib
    which needs bytes, so those tests pass plistlib.dumps() directly.
    """
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


MOUNT_OUTPUT_TEMPLATE = """\
/dev/disk1s1 on / (apfs, local, read-only, journaled)
/dev/disk3s1 on /Volumes/NO_NAME (msdos, local, nodev, nosuid, noowners)
"""

DISKUTIL_PLIST_EXTERNAL_EXFAT = {
    "AllDisksAndPartitions": [
        {
            "DeviceIdentifier": "disk4",
            "Internal": False,
            "Partitions": [
                {
                    "DeviceIdentifier": "disk4s1",
                    "Content": "Microsoft Basic Data",
                },
            ],
        }
    ]
}

DISKUTIL_PLIST_INTERNAL = {
    "AllDisksAndPartitions": [
        {
            "DeviceIdentifier": "disk0",
            "Internal": True,
            "Partitions": [
                {
                    "DeviceIdentifier": "disk0s1",
                    "Content": "Microsoft Basic Data",
                }
            ],
        }
    ]
}

DISKUTIL_PLIST_EFI = {
    "AllDisksAndPartitions": [
        {
            "DeviceIdentifier": "disk4",
            "Internal": False,
            "Partitions": [
                {
                    "DeviceIdentifier": "disk4s1",
                    "Content": "EFI",
                },
                {
                    "DeviceIdentifier": "disk4s2",
                    "Content": "Microsoft Basic Data",
                },
            ],
        }
    ]
}

DISKUTIL_PLIST_APFS = {
    "AllDisksAndPartitions": [
        {
            "DeviceIdentifier": "disk4",
            "Internal": False,
            "Partitions": [
                {
                    "DeviceIdentifier": "disk4s1",
                    "Content": "Apple_APFS",
                },
            ],
        }
    ]
}

DISKUTIL_PLIST_MULTI = {
    "AllDisksAndPartitions": [
        {
            "DeviceIdentifier": "disk4",
            "Internal": False,
            "Partitions": [
                {"DeviceIdentifier": "disk4s1", "Content": "EFI"},
                {"DeviceIdentifier": "disk4s2", "Content": "Microsoft Basic Data"},
                {"DeviceIdentifier": "disk4s3", "Content": "DOS_FAT_32"},
                {"DeviceIdentifier": "disk4s4", "Content": "Windows_NTFS"},
            ],
        },
        {
            "DeviceIdentifier": "disk5",
            "Internal": False,
            "Partitions": [
                {"DeviceIdentifier": "disk5s1", "Content": "exFAT"},
                {"DeviceIdentifier": "disk5s2", "Content": "ExFAT"},
                {"DeviceIdentifier": "disk5s3", "Content": "exfat"},
            ],
        },
    ]
}


# ===========================================================================
# TestDitMateVersion
# ===========================================================================


class TestDitMateVersion:
    def test_version_is_a_non_empty_string(self):
        """__version__ must be a non-empty string after module load."""
        assert isinstance(lifsaver.__version__, str)
        assert lifsaver.__version__ != ""

    def test_missing_version_key_raises_key_error(self):
        """A toml without [project].version must raise KeyError, not silently return None."""
        data = tomllib.loads('[project]\nname = "dit-mate"')
        with pytest.raises(KeyError):
            _ = data["project"]["version"]

    def test_version_flag_exits_zero(self):
        """--version must exit with code 0."""
        with patch.object(sys, "argv", ["lifsaver", "--version"]), pytest.raises(SystemExit) as exc_info:
            lifsaver.parse_args()
        assert exc_info.value.code == 0

    def test_version_flag_output_contains_version(self, capsys):
        """--version output must include the version string loaded at import time."""
        with patch.object(sys, "argv", ["lifsaver", "--version"]), pytest.raises(SystemExit):
            lifsaver.parse_args()
        out = capsys.readouterr().out
        assert lifsaver.__version__ in out

    def test_version_flag_output_contains_script_name(self, capsys):
        """--version output must be a bare number."""
        with patch.object(sys, "argv", ["lifsaver", "--version"]), pytest.raises(SystemExit):
            lifsaver.parse_args()
        out = capsys.readouterr().out
        assert out.strip() == lifsaver.__version__


# ===========================================================================
# TestCheckPlatform
# ===========================================================================


class TestCheckPlatform:
    """Unit tests for the check_platform() guard function itself."""

    def test_passes_on_darwin(self):
        with patch.object(sys, "platform", "darwin"):
            lifsaver.check_platform()  # must not raise or exit

    @pytest.mark.parametrize("plat", ["linux", "win32", "freebsd7"])
    def test_exits_on_non_macos(self, plat):
        with patch.object(sys, "platform", plat):
            with pytest.raises(SystemExit) as exc_info:
                lifsaver.check_platform()
            assert exc_info.value.code == 1

    def test_unknown_platform_no_crash(self, capsys):
        with patch.object(sys, "platform", "haiku"), pytest.raises(SystemExit):
            lifsaver.check_platform()
        err = capsys.readouterr().err
        assert "haiku" in err

    def test_message_mentions_macos(self, capsys):
        """The error must make clear this is a macOS-only tool."""
        with patch.object(sys, "platform", "linux"), pytest.raises(SystemExit):
            lifsaver.check_platform()
        err = capsys.readouterr().err
        assert "macOS" in err

    def test_detected_platform_name_appears_in_message(self, capsys):
        """User should see their own platform name so the message is actionable."""
        with patch.object(sys, "platform", "win32"), pytest.raises(SystemExit):
            lifsaver.check_platform()
        err = capsys.readouterr().err
        assert "win32" in err

    def test_ci_bypass_exits_zero_on_non_macos(self, monkeypatch):
        """When CI=true, the script must exit 0 even on a restricted OS to allow smoke tests."""
        # Simulate a non-macOS platform
        monkeypatch.setattr(sys, "platform", "linux")

        # Inject the CI environment variable
        monkeypatch.setenv("CI", "true")

        # Assert the bypass intercepts the restriction and exits cleanly
        with pytest.raises(SystemExit) as exc_info:
            lifsaver.check_platform()

        assert exc_info.value.code == 0


# ===========================================================================
# TestMainPlatformGuard
# ===========================================================================


class TestMainPlatformGuard:
    """
    Integration tests: verify that main() is completely inert on non-macOS.

    These tests prove that:
      - check_platform() is called before anything else in main()
      - no disk introspection, no mounting, no sudo re-exec happens
      - the exit code is exactly 1
      - a human-readable message is printed to stderr
    """

    NON_MACOS_PLATFORMS = ("linux", "win32", "freebsd7", "haiku")

    @pytest.mark.parametrize("plat", NON_MACOS_PLATFORMS)
    def test_main_exits_with_code_1_on_non_macos(self, plat):
        with patch.object(sys, "platform", plat), pytest.raises(SystemExit) as exc_info:
            lifsaver.main()
        assert exc_info.value.code == 1

    @pytest.mark.parametrize("plat", NON_MACOS_PLATFORMS)
    def test_main_prints_informative_message_to_stderr(self, plat, capsys):
        with patch.object(sys, "platform", plat), pytest.raises(SystemExit):
            lifsaver.main()
        err = capsys.readouterr().err
        assert "macOS" in err
        assert plat in err

    @pytest.mark.parametrize("plat", NON_MACOS_PLATFORMS)
    def test_main_does_not_call_diskutil_on_non_macos(self, plat):
        """diskutil must never be invoked outside macOS."""
        with (
            patch.object(sys, "platform", plat),
            patch("subprocess.run") as mock_run,
            pytest.raises(SystemExit),
        ):
            lifsaver.main()

        called_cmds = [str(c) for c in mock_run.call_args_list]
        assert not any("diskutil" in cmd for cmd in called_cmds), f"diskutil was called on {plat}: {called_cmds}"

    @pytest.mark.parametrize("plat", NON_MACOS_PLATFORMS)
    def test_main_does_not_attempt_any_mount_on_non_macos(self, plat):
        """No mount strategy should be attempted outside macOS."""
        with (
            patch.object(sys, "platform", plat),
            patch.object(lifsaver, "execute_mount") as mock_mount,
            pytest.raises(SystemExit),
        ):
            lifsaver.main()

        mock_mount.assert_not_called()

    @pytest.mark.parametrize("plat", NON_MACOS_PLATFORMS)
    def test_main_does_not_call_sudo_on_non_macos(self, plat):
        """os.execvp (sudo re-exec) must not be reached on non-macOS."""
        with patch.object(sys, "platform", plat), patch("os.execvp") as mock_execvp, pytest.raises(SystemExit):
            lifsaver.main()
        mock_execvp.assert_not_called()

    @pytest.mark.parametrize("plat", NON_MACOS_PLATFORMS)
    def test_main_produces_no_stdout_on_non_macos(self, plat, capsys):
        """Normal stdout (banners, progress) must be silent; message goes to stderr only."""
        with patch.object(sys, "platform", plat), pytest.raises(SystemExit):
            lifsaver.main()
        out = capsys.readouterr().out
        assert out == ""


# ===========================================================================
# TestMainHappyPath
# ===========================================================================


class TestMainHappyPath:
    """Verify main()'s wiring on (mocked) darwin: sudo guard → scan → filter → mount → summary."""

    DISK_DATA: ClassVar[dict] = {"AllDisksAndPartitions": []}

    def test_mounts_each_target_with_forwarded_flags(self, capsys):
        with (
            patch.object(sys, "platform", "darwin"),
            patch.object(sys, "argv", ["lifsaver", "--verbose"]),
            patch("os.getuid", return_value=0),
            patch.object(lifsaver, "get_disk_data", return_value=self.DISK_DATA),
            patch.object(lifsaver, "filter_target_partitions", return_value=["disk4s1", "disk5s2"]),
            patch.object(lifsaver, "execute_mount", return_value="ok") as mock_mount,
        ):
            lifsaver.main()

        assert mock_mount.call_args_list == [
            call("disk4s1", dry_run=False, verbose=True),
            call("disk5s2", dry_run=False, verbose=True),
        ]
        assert "2 mounted, 0 failed, 0 skipped" in capsys.readouterr().out

    def test_disk_data_is_passed_to_filter(self):
        with (
            patch.object(sys, "platform", "darwin"),
            patch.object(sys, "argv", ["lifsaver"]),
            patch("os.getuid", return_value=0),
            patch.object(lifsaver, "get_disk_data", return_value=self.DISK_DATA),
            patch.object(lifsaver, "filter_target_partitions", return_value=[]) as mock_filter,
        ):
            lifsaver.main()

        mock_filter.assert_called_once_with(self.DISK_DATA)

    def test_no_targets_prints_message_without_mounting(self, capsys):
        with (
            patch.object(sys, "platform", "darwin"),
            patch.object(sys, "argv", ["lifsaver"]),
            patch("os.getuid", return_value=0),
            patch.object(lifsaver, "get_disk_data", return_value=self.DISK_DATA),
            patch.object(lifsaver, "filter_target_partitions", return_value=[]),
            patch.object(lifsaver, "execute_mount") as mock_mount,
        ):
            lifsaver.main()

        mock_mount.assert_not_called()
        assert "No stalled or unmounted camera data volumes detected." in capsys.readouterr().out

    def test_non_root_reexecs_with_sudo_preserving_argv(self):
        with (
            patch.object(sys, "platform", "darwin"),
            patch.object(sys, "argv", ["lifsaver", "--verbose"]),
            patch("os.getuid", return_value=501),
            patch("os.execvp") as mock_execvp,
            patch.object(lifsaver, "get_disk_data", return_value=self.DISK_DATA),
            patch.object(lifsaver, "filter_target_partitions", return_value=[]),
        ):
            lifsaver.main()

        mock_execvp.assert_called_once_with("sudo", ["sudo", sys.executable, "lifsaver", "--verbose"])

    def test_root_does_not_reexec(self):
        with (
            patch.object(sys, "platform", "darwin"),
            patch.object(sys, "argv", ["lifsaver"]),
            patch("os.getuid", return_value=0),
            patch("os.execvp") as mock_execvp,
            patch.object(lifsaver, "get_disk_data", return_value=self.DISK_DATA),
            patch.object(lifsaver, "filter_target_partitions", return_value=[]),
        ):
            lifsaver.main()

        mock_execvp.assert_not_called()

    def test_dry_run_as_non_root_does_not_reexec(self):
        """Dry-run only performs read-only queries, so it must never escalate to root."""
        with (
            patch.object(sys, "platform", "darwin"),
            patch.object(sys, "argv", ["lifsaver", "--dry-run"]),
            patch("os.getuid", return_value=501),
            patch("os.execvp") as mock_execvp,
            patch.object(lifsaver, "get_disk_data", return_value=self.DISK_DATA),
            patch.object(lifsaver, "filter_target_partitions", return_value=[]),
        ):
            lifsaver.main()

        mock_execvp.assert_not_called()


# ===========================================================================
# get_active_mounts
# ===========================================================================


class TestGetActiveMounts:
    def test_parses_dev_entries(self):
        with patch("subprocess.run", return_value=_completed(stdout=MOUNT_OUTPUT_TEMPLATE)):
            result = lifsaver.get_active_mounts()
        assert "/dev/disk1s1" in result
        assert "/dev/disk3s1" in result

    def test_ignores_non_dev_lines(self):
        mount_out = "map auto_home on /home (autofs, ...)\n/dev/disk1s1 on / (apfs)\n"
        with patch("subprocess.run", return_value=_completed(stdout=mount_out)):
            result = lifsaver.get_active_mounts()
        assert "/dev/disk1s1" in result
        assert len(result) == 1

    def test_returns_empty_set_on_failure(self, capsys):
        with patch("subprocess.run", side_effect=OSError("boom")):
            result = lifsaver.get_active_mounts()
        assert result == set()
        assert "WARNING" in capsys.readouterr().err

    def test_returns_empty_set_on_empty_output(self):
        with patch("subprocess.run", return_value=_completed(stdout="")):
            result = lifsaver.get_active_mounts()
        assert result == set()


# ===========================================================================
# is_currently_mounted
# ===========================================================================


class TestIsCurrentlyMounted:
    def test_true_when_present(self):
        with patch.object(lifsaver, "get_active_mounts", return_value={"/dev/disk4s1"}):
            assert lifsaver.is_currently_mounted("disk4s1") is True

    def test_false_when_absent(self):
        with patch.object(lifsaver, "get_active_mounts", return_value={"/dev/disk1s1"}):
            assert lifsaver.is_currently_mounted("disk4s1") is False

    def test_always_calls_fresh_mount_query(self):
        """Must never rely on a cached set — each call must hit get_active_mounts."""
        with patch.object(lifsaver, "get_active_mounts", return_value=set()) as mock_game:
            lifsaver.is_currently_mounted("disk4s1")
            lifsaver.is_currently_mounted("disk4s1")
        assert mock_game.call_count == 2


# ===========================================================================
# is_fsck_active
# ===========================================================================


class TestIsFsckActive:
    def test_true_when_fsck_targets_device(self):
        pgrep_out = "812 /System/Library/Filesystems/exfat.fs/Contents/Resources/fsck_exfat -y /dev/rdisk4s1\n"
        with patch("subprocess.run", return_value=_completed(stdout=pgrep_out)):
            assert lifsaver.is_fsck_active("disk4s1") is True

    def test_matches_non_raw_device_node(self):
        pgrep_out = "812 fsck_msdos -y /dev/disk4s1\n"
        with patch("subprocess.run", return_value=_completed(stdout=pgrep_out)):
            assert lifsaver.is_fsck_active("disk4s1") is True

    def test_false_when_fsck_targets_other_device(self):
        pgrep_out = "812 fsck_exfat -y /dev/rdisk5s1\n"
        with patch("subprocess.run", return_value=_completed(stdout=pgrep_out)):
            assert lifsaver.is_fsck_active("disk4s1") is False

    def test_no_false_match_on_longer_identifier(self):
        """disk4s1 must not match a check running on disk4s10."""
        pgrep_out = "812 fsck_exfat -y /dev/rdisk4s10\n"
        with patch("subprocess.run", return_value=_completed(stdout=pgrep_out)):
            assert lifsaver.is_fsck_active("disk4s1") is False

    def test_no_false_match_inside_longer_token(self):
        """disk4s1 must not match in the middle of an unrelated word like 'mydisk4s1'."""
        pgrep_out = "812 fsck_exfat -y /dev/mydisk4s1\n"
        with patch("subprocess.run", return_value=_completed(stdout=pgrep_out)):
            assert lifsaver.is_fsck_active("disk4s1") is False

    def test_false_when_no_fsck_running(self):
        with patch("subprocess.run", return_value=_completed(returncode=1, stdout="")):
            assert lifsaver.is_fsck_active("disk4s1") is False

    def test_false_on_pgrep_failure(self):
        with patch("subprocess.run", side_effect=OSError("boom")):
            assert lifsaver.is_fsck_active("disk4s1") is False


# ===========================================================================
# get_disk_data
# ===========================================================================


class TestGetDiskData:
    def test_returns_parsed_plist(self):
        raw = plistlib.dumps(DISKUTIL_PLIST_EXTERNAL_EXFAT)
        mock_result = MagicMock()
        mock_result.stdout = raw
        with patch("subprocess.run", return_value=mock_result):
            data = lifsaver.get_disk_data()
        assert "AllDisksAndPartitions" in data

    def test_exits_on_diskutil_failure(self):
        with (
            patch(
                "subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "diskutil"),
            ),
            pytest.raises(SystemExit),
        ):
            lifsaver.get_disk_data()


# ===========================================================================
# get_partition_fs_type
# ===========================================================================


class TestGetPartitionFsType:
    def test_returns_filesystem_type_lowercase(self):
        info = {"FilesystemType": "ExFAT", "Content": "Microsoft Basic Data"}
        mock_result = MagicMock()
        mock_result.stdout = plistlib.dumps(info)
        with patch("subprocess.run", return_value=mock_result):
            assert lifsaver.get_partition_fs_type("disk4s1") == "exfat"

    def test_falls_back_to_content_when_no_filesystem_type(self):
        info = {"Content": "DOS_FAT_32"}
        mock_result = MagicMock()
        mock_result.stdout = plistlib.dumps(info)
        with patch("subprocess.run", return_value=mock_result):
            assert lifsaver.get_partition_fs_type("disk4s1") == "dos_fat_32"

    def test_returns_empty_string_on_failure(self):
        with patch("subprocess.run", side_effect=OSError("boom")):
            assert lifsaver.get_partition_fs_type("disk4s1") == ""

    def test_returns_empty_string_when_both_keys_missing(self):
        mock_result = MagicMock()
        mock_result.stdout = plistlib.dumps({"SomeOtherKey": "value"})
        with patch("subprocess.run", return_value=mock_result):
            assert lifsaver.get_partition_fs_type("disk4s1") == ""


# ===========================================================================
# filter_target_partitions
# ===========================================================================


class TestFilterTargetPartitions:
    def _patch_mounts(self, mounted=None):
        return patch.object(lifsaver, "get_active_mounts", return_value=set(mounted or []))

    def test_picks_up_unmounted_external_exfat(self):
        with self._patch_mounts():
            targets = lifsaver.filter_target_partitions(DISKUTIL_PLIST_EXTERNAL_EXFAT)
        assert targets == ["disk4s1"]

    def test_skips_internal_disks(self):
        with self._patch_mounts():
            targets = lifsaver.filter_target_partitions(DISKUTIL_PLIST_INTERNAL)
        assert targets == []

    def test_skips_efi_partition(self):
        with self._patch_mounts():
            targets = lifsaver.filter_target_partitions(DISKUTIL_PLIST_EFI)
        # disk4s1 = EFI (blocked), disk4s2 = Microsoft Basic Data (allowed)
        assert "disk4s1" not in targets
        assert "disk4s2" in targets

    def test_skips_apple_apfs(self):
        with self._patch_mounts():
            targets = lifsaver.filter_target_partitions(DISKUTIL_PLIST_APFS)
        assert targets == []

    def test_skips_already_mounted_device(self, capsys):
        with self._patch_mounts(mounted=["/dev/disk4s1"]):
            targets = lifsaver.filter_target_partitions(DISKUTIL_PLIST_EXTERNAL_EXFAT)
        assert targets == []
        assert "already mounted" in capsys.readouterr().out

    def test_multi_disk_multi_partition(self):
        # disk4s1=EFI(skip), disk4s2=MBD(ok), disk4s3=DOS_FAT_32(ok),
        # disk4s4=Windows_NTFS(ok — exFAT and NTFS share MBR type 0x07),
        # disk5: personality-name variants; only allowlisted spellings match
        with self._patch_mounts():
            targets = lifsaver.filter_target_partitions(DISKUTIL_PLIST_MULTI)
        assert "disk4s1" not in targets
        assert "disk4s2" in targets
        assert "disk4s3" in targets
        assert "disk4s4" in targets
        assert "disk5s1" in targets
        assert "disk5s2" in targets
        assert "disk5s3" not in targets

    def test_empty_disk_data_returns_empty(self):
        with self._patch_mounts():
            targets = lifsaver.filter_target_partitions({"AllDisksAndPartitions": []})
        assert targets == []

    def test_partition_missing_device_identifier_is_skipped(self):
        data = {
            "AllDisksAndPartitions": [
                {
                    "Internal": False,
                    "Partitions": [{"Content": "Microsoft Basic Data"}],  # no DeviceIdentifier
                }
            ]
        }
        with self._patch_mounts():
            targets = lifsaver.filter_target_partitions(data)
        assert targets == []

    @pytest.mark.parametrize(
        "content_type",
        [
            "Microsoft Basic Data",
            "DOS_FAT_32",
            "Windows_FAT_32",
            "Windows_NTFS",
            "exFAT",
            "ExFAT",
        ],
    )
    def test_all_allowlisted_content_types_are_accepted(self, content_type):
        data = {
            "AllDisksAndPartitions": [
                {
                    "Internal": False,
                    "Partitions": [{"DeviceIdentifier": "disk4s1", "Content": content_type}],
                }
            ]
        }
        with self._patch_mounts():
            targets = lifsaver.filter_target_partitions(data)
        assert "disk4s1" in targets

    @pytest.mark.parametrize(
        "content_type",
        [
            "Apple_APFS",
            "Apple_HFS",
            "Apple_Boot",
            "Apple_Recovery",
            "Apple_CoreStorage",
            "EFI",
        ],
    )
    def test_all_blocklisted_content_types_are_rejected(self, content_type):
        data = {
            "AllDisksAndPartitions": [
                {
                    "Internal": False,
                    "Partitions": [{"DeviceIdentifier": "disk4s1", "Content": content_type}],
                }
            ]
        }
        with self._patch_mounts():
            targets = lifsaver.filter_target_partitions(data)
        assert targets == []


# ===========================================================================
# _run_diskutil_mount
# ===========================================================================


class TestRunDiskutilMount:
    def test_returns_true_on_success(self):
        with patch("subprocess.run", return_value=_completed(returncode=0)):
            assert lifsaver._run_diskutil_mount("disk4s1", verbose=False) is True

    def test_returns_false_on_failure(self):
        with patch("subprocess.run", return_value=_completed(returncode=1)):
            assert lifsaver._run_diskutil_mount("disk4s1", verbose=False) is False

    def test_prints_stderr_when_verbose(self, capsys):
        with patch("subprocess.run", return_value=_completed(returncode=1, stderr="oops")):
            lifsaver._run_diskutil_mount("disk4s1", verbose=True)
        assert "oops" in capsys.readouterr().err

    def test_silent_on_stderr_when_not_verbose(self, capsys):
        with patch("subprocess.run", return_value=_completed(returncode=1, stderr="oops")):
            lifsaver._run_diskutil_mount("disk4s1", verbose=False)
        assert capsys.readouterr().err == ""


# ===========================================================================
# _run_raw_mount
# ===========================================================================


class TestRunRawMount:
    def test_exfat_tried_first_for_unknown_fs(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd[0])
            return _completed(returncode=0)

        with patch("subprocess.run", side_effect=fake_run), patch("pathlib.Path.mkdir"):
            lifsaver._run_raw_mount("disk4s1", fs_type="", verbose=False)

        assert "mount_exfat" in calls[0]

    def test_msdos_tried_first_for_fat32(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd[0])
            return _completed(returncode=0)

        with patch("subprocess.run", side_effect=fake_run), patch("pathlib.Path.mkdir"):
            lifsaver._run_raw_mount("disk4s1", fs_type="msdos", verbose=False)

        assert "mount_msdos" in calls[0]

    def test_falls_back_to_second_binary(self):
        results = [_completed(returncode=1), _completed(returncode=0)]

        with patch("subprocess.run", side_effect=results), patch("pathlib.Path.mkdir"):
            result = lifsaver._run_raw_mount("disk4s1", fs_type="", verbose=False)

        assert result is True

    def test_returns_false_when_both_fail(self):
        with (
            patch("subprocess.run", return_value=_completed(returncode=1)),
            patch("pathlib.Path.mkdir"),
            patch("pathlib.Path.rmdir") as mock_rmdir,
        ):
            result = lifsaver._run_raw_mount("disk4s1", fs_type="", verbose=False)

        assert result is False
        mock_rmdir.assert_called_once()

    def test_returns_false_when_mount_point_creation_fails(self, capsys):
        with (
            patch("pathlib.Path.mkdir", side_effect=PermissionError("read-only /Volumes")),
            patch("subprocess.run") as mock_run,
        ):
            result = lifsaver._run_raw_mount("disk4s1", fs_type="", verbose=True)

        assert result is False
        mock_run.assert_not_called()
        assert "read-only /Volumes" in capsys.readouterr().err

    def test_rmdir_not_called_on_success(self):
        with (
            patch("subprocess.run", return_value=_completed(returncode=0)),
            patch("pathlib.Path.mkdir"),
            patch("pathlib.Path.rmdir") as mock_rmdir,
        ):
            lifsaver._run_raw_mount("disk4s1", fs_type="", verbose=False)

        mock_rmdir.assert_not_called()

    def test_verbose_stderr_printed_on_failure(self, capsys):
        with (
            patch("subprocess.run", return_value=_completed(returncode=1, stderr="bad device")),
            patch("pathlib.Path.mkdir"),
            patch("pathlib.Path.rmdir"),
        ):
            lifsaver._run_raw_mount("disk4s1", fs_type="", verbose=True)

        assert "bad device" in capsys.readouterr().err


# ===========================================================================
# execute_mount
# ===========================================================================


class TestExecuteMount:
    def test_dry_run_returns_ok_without_mounting(self):
        with (
            patch.object(lifsaver, "is_currently_mounted", return_value=False),
            patch.object(lifsaver, "is_fsck_active", return_value=False),
            patch.object(lifsaver, "get_partition_fs_type", return_value="exfat"),
            patch.object(lifsaver, "_run_diskutil_mount") as mock_du,
        ):
            result = lifsaver.execute_mount("disk4s1", dry_run=True)

        assert result == "ok"
        mock_du.assert_not_called()

    def test_skips_if_mounted_since_scan(self, capsys):
        with patch.object(lifsaver, "is_currently_mounted", return_value=True):
            result = lifsaver.execute_mount("disk4s1")
        assert result == "skip"
        assert "SKIPPED" in capsys.readouterr().out

    def test_skips_when_fsck_is_active(self, capsys):
        with (
            patch.object(lifsaver, "is_currently_mounted", return_value=False),
            patch.object(lifsaver, "is_fsck_active", return_value=True),
            patch.object(lifsaver, "_run_diskutil_mount") as mock_du,
            patch.object(lifsaver, "_run_raw_mount") as mock_raw,
        ):
            result = lifsaver.execute_mount("disk4s1")

        assert result == "skip"
        assert "consistency check" in capsys.readouterr().out
        mock_du.assert_not_called()
        mock_raw.assert_not_called()

    def test_fsck_gate_applies_to_dry_run_too(self, capsys):
        with (
            patch.object(lifsaver, "is_currently_mounted", return_value=False),
            patch.object(lifsaver, "is_fsck_active", return_value=True),
        ):
            result = lifsaver.execute_mount("disk4s1", dry_run=True)

        assert result == "skip"
        assert "fsck" in capsys.readouterr().out

    def test_succeeds_via_diskutil(self):
        mounted_sequence = [False, True]  # unmounted at check, mounted after diskutil

        with (
            patch.object(lifsaver, "is_currently_mounted", side_effect=mounted_sequence),
            patch.object(lifsaver, "is_fsck_active", return_value=False),
            patch.object(lifsaver, "get_partition_fs_type", return_value="exfat"),
            patch.object(lifsaver, "_run_diskutil_mount", return_value=True),
            patch.object(lifsaver, "_find_mount_point", return_value="/Volumes/CARD"),
        ):
            result = lifsaver.execute_mount("disk4s1")

        assert result == "ok"

    def test_falls_back_to_raw_mount_when_diskutil_fails(self):
        # Call sequence for is_currently_mounted:
        #   1. race-guard check → False (not yet mounted, proceed)
        #   2. post-raw-mount verify → True (raw mount succeeded)
        # Note: when _run_diskutil_mount returns False, its inner
        # is_currently_mounted check is never reached (short-circuit).
        mounted_sequence = [False, True]

        with (
            patch.object(lifsaver, "is_currently_mounted", side_effect=mounted_sequence),
            patch.object(lifsaver, "is_fsck_active", return_value=False),
            patch.object(lifsaver, "get_partition_fs_type", return_value=""),
            patch.object(lifsaver, "_run_diskutil_mount", return_value=False),
            patch.object(lifsaver, "_run_raw_mount", return_value=True),
        ):
            result = lifsaver.execute_mount("disk4s1")

        assert result == "ok"

    def test_returns_fail_when_all_strategies_fail(self, capsys):
        with (
            patch.object(lifsaver, "is_currently_mounted", return_value=False),
            patch.object(lifsaver, "is_fsck_active", return_value=False),
            patch.object(lifsaver, "get_partition_fs_type", return_value=""),
            patch.object(lifsaver, "_run_diskutil_mount", return_value=False),
            patch.object(lifsaver, "_run_raw_mount", return_value=False),
        ):
            result = lifsaver.execute_mount("disk4s1")

        assert result == "fail"
        assert "CRITICAL ERROR" in capsys.readouterr().out


# ===========================================================================
# main summary / exit code
# ===========================================================================


class TestMainSummary:
    def _run_main(self, argv, outcome):
        with (
            patch.object(sys, "platform", "darwin"),
            patch.object(sys, "argv", argv),
            patch("os.getuid", return_value=0),
            patch.object(lifsaver, "get_disk_data", return_value={}),
            patch.object(lifsaver, "filter_target_partitions", return_value=["disk4s1"]),
            patch.object(lifsaver, "execute_mount", return_value=outcome),
        ):
            lifsaver.main()

    def test_dry_run_summary_says_mountable(self, capsys):
        """Dry-run must not claim volumes were mounted or could have failed."""
        self._run_main(["lifsaver", "--dry-run"], outcome="ok")
        out = capsys.readouterr().out
        assert "1 mountable" in out
        assert "mounted" not in out
        assert "failed" not in out

    def test_real_run_summary_says_mounted(self, capsys):
        self._run_main(["lifsaver"], outcome="ok")
        assert "1 mounted" in capsys.readouterr().out

    def test_exits_nonzero_when_a_mount_fails(self):
        with pytest.raises(SystemExit) as exc_info:
            self._run_main(["lifsaver"], outcome="fail")
        assert exc_info.value.code == 1

    def test_exits_zero_when_all_mounts_succeed(self):
        self._run_main(["lifsaver"], outcome="ok")  # must not raise SystemExit


# ===========================================================================
# _find_mount_point
# ===========================================================================


class TestFindMountPoint:
    def test_extracts_correct_mount_point(self):
        mount_out = "/dev/disk4s1 on /Volumes/CARD (exfat, local)\n"
        with patch("subprocess.run", return_value=_completed(stdout=mount_out)):
            result = lifsaver._find_mount_point("disk4s1")
        assert result == "/Volumes/CARD"

    def test_returns_empty_string_when_not_found(self):
        with patch("subprocess.run", return_value=_completed(stdout=MOUNT_OUTPUT_TEMPLATE)):
            result = lifsaver._find_mount_point("disk9s9")
        assert result == ""

    def test_returns_empty_string_on_exception(self):
        with patch("subprocess.run", side_effect=OSError("boom")):
            result = lifsaver._find_mount_point("disk4s1")
        assert result == ""

    def test_handles_mount_point_with_spaces(self):
        mount_out = "/dev/disk4s1 on /Volumes/My Card (exfat, local)\n"
        with patch("subprocess.run", return_value=_completed(stdout=mount_out)):
            result = lifsaver._find_mount_point("disk4s1")
        assert result == "/Volumes/My Card"
