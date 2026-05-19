"""
pytest suite for lifsaver.py
================================

Run with:
    uv run pytest
"""

import plistlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, mock_open, patch

import pytest
import tomllib

# ---------------------------------------------------------------------------
# Import the module under test.  We patch sys.platform so the module-level
# code (if any) doesn't blow up on non-macOS runners.
# ---------------------------------------------------------------------------
with patch.dict("sys.modules", {}):
    import importlib, types
    # Force a clean import every time tests are collected.
    if "lifsaver" in sys.modules:
        del sys.modules["force_mount"]

import importlib.util, os

_SCRIPT = Path(__file__).parent / "force-mount.py"
_spec   = importlib.util.spec_from_file_location("force_mount", _SCRIPT)
force_mount = importlib.util.module_from_spec(_spec)
# Stub the version loader before exec so __version__ = _dit_mate_version()
# at module level doesn't require a real pyproject.toml on the test runner.
with patch("builtins.open", mock_open(read_data=b'[project]\nversion = "0.0.0-test"\n')):
    _spec.loader.exec_module(force_mount)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _completed(returncode=0, stdout="", stderr=""):
    """
    Build a fake CompletedProcess.

    stdout is kept as str because every caller in force-mount.py that reads
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

MOUNT_OUTPUT_WITH_CARD = """\
/dev/disk1s1 on / (apfs, local, read-only, journaled)
/dev/disk4s1 on /Volumes/CAMERA (exfat, local, nodev, nosuid, noowners)
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
                {"DeviceIdentifier": "disk4s3", "Content": "DOS_FAT"},
            ],
        },
        {
            "DeviceIdentifier": "disk5",
            "Internal": False,
            "Partitions": [
                {"DeviceIdentifier": "disk5s1", "Content": "exfat"},
            ],
        },
    ]
}


# ===========================================================================
# check_platform
# ===========================================================================

# ===========================================================================
# _dit_mate_version / __version__
# ===========================================================================

class TestDitMateVersion:
    def test_version_is_a_non_empty_string(self):
        """__version__ must be a non-empty string after module load."""
        assert isinstance(force_mount.__version__, str)
        assert force_mount.__version__ != ""

    def test_version_value_matches_toml(self, tmp_path):
        """_dit_mate_version() reads the version field from a real toml file."""
        (tmp_path / "pyproject.toml").write_bytes(b'[project]\nversion = "9.8.7"\n')
        with patch.object(force_mount, "_dit_mate_version",
                          lambda: tomllib.loads("[project]\nversion = \"9.8.7\"")["project"]["version"]):
            assert force_mount._dit_mate_version() == "9.8.7"

    def test_missing_version_key_raises_key_error(self, tmp_path):
        """A toml without [project].version must raise KeyError, not silently return None."""
        data = tomllib.loads("[project]\nname = \"dit-mate\"")
        with pytest.raises(KeyError):
            _ = data["project"]["version"]

    def test_version_flag_exits_zero(self):
        """--version must exit with code 0."""
        with patch.object(sys, "argv", ["force-mount", "--version"]):
            with pytest.raises(SystemExit) as exc_info:
                force_mount.parse_args()
        assert exc_info.value.code == 0

    def test_version_flag_output_contains_version(self, capsys):
        """--version output must include the version string loaded at import time."""
        with patch.object(sys, "argv", ["force-mount", "--version"]):
            with pytest.raises(SystemExit):
                force_mount.parse_args()
        out = capsys.readouterr().out
        assert force_mount.__version__ in out

    def test_version_flag_output_contains_script_name(self, capsys):
        """--version output must include the script name, not just a bare number."""
        with patch.object(sys, "argv", ["force-mount", "--version"]):
            with pytest.raises(SystemExit):
                force_mount.parse_args()
        out = capsys.readouterr().out
        assert "force-mount" in out


class TestCheckPlatform:
    """Unit tests for the check_platform() guard function itself."""

    def test_passes_on_darwin(self):
        with patch.object(sys, "platform", "darwin"):
            force_mount.check_platform()  # must not raise or exit

    @pytest.mark.parametrize("plat", ["linux", "win32", "freebsd7"])
    def test_exits_on_non_macos(self, plat):
        with patch.object(sys, "platform", plat):
            with pytest.raises(SystemExit) as exc_info:
                force_mount.check_platform()
            assert exc_info.value.code == 1

    def test_linux_hint_in_stderr(self, capsys):
        with patch.object(sys, "platform", "linux"):
            with pytest.raises(SystemExit):
                force_mount.check_platform()
        err = capsys.readouterr().err
        assert "udisksctl" in err

    def test_windows_hint_in_stderr(self, capsys):
        with patch.object(sys, "platform", "win32"):
            with pytest.raises(SystemExit):
                force_mount.check_platform()
        err = capsys.readouterr().err
        assert "mountvol" in err

    def test_unknown_platform_no_hint_no_crash(self, capsys):
        with patch.object(sys, "platform", "haiku"):
            with pytest.raises(SystemExit):
                force_mount.check_platform()
        err = capsys.readouterr().err
        assert "haiku" in err

    def test_message_mentions_macos(self, capsys):
        """The error must make clear this is a macOS-only tool."""
        with patch.object(sys, "platform", "linux"):
            with pytest.raises(SystemExit):
                force_mount.check_platform()
        err = capsys.readouterr().err
        assert "macOS" in err

    def test_detected_platform_name_appears_in_message(self, capsys):
        """User should see their own platform name so the message is actionable."""
        with patch.object(sys, "platform", "win32"):
            with pytest.raises(SystemExit):
                force_mount.check_platform()
        err = capsys.readouterr().err
        assert "win32" in err


class TestMainPlatformGuard:
    """
    Integration tests: verify that main() is completely inert on non-macOS.

    These tests prove that:
      - check_platform() is called before anything else in main()
      - no disk introspection, no mounting, no sudo re-exec happens
      - the exit code is exactly 1
      - a human-readable message is printed to stderr
    """

    NON_MACOS_PLATFORMS = ["linux", "win32", "freebsd7", "haiku"]

    @pytest.mark.parametrize("plat", NON_MACOS_PLATFORMS)
    def test_main_exits_with_code_1_on_non_macos(self, plat):
        with patch.object(sys, "platform", plat):
            with pytest.raises(SystemExit) as exc_info:
                force_mount.main()
        assert exc_info.value.code == 1

    @pytest.mark.parametrize("plat", NON_MACOS_PLATFORMS)
    def test_main_prints_informative_message_to_stderr(self, plat, capsys):
        with patch.object(sys, "platform", plat):
            with pytest.raises(SystemExit):
                force_mount.main()
        err = capsys.readouterr().err
        assert "macOS" in err
        assert plat in err

    @pytest.mark.parametrize("plat", NON_MACOS_PLATFORMS)
    def test_main_does_not_call_diskutil_on_non_macos(self, plat):
        """diskutil must never be invoked outside macOS."""
        with patch.object(sys, "platform", plat):
            with patch("subprocess.run") as mock_run:
                with pytest.raises(SystemExit):
                    force_mount.main()
        called_cmds = [str(c) for c in mock_run.call_args_list]
        assert not any("diskutil" in cmd for cmd in called_cmds), (
            f"diskutil was called on {plat}: {called_cmds}"
        )

    @pytest.mark.parametrize("plat", NON_MACOS_PLATFORMS)
    def test_main_does_not_attempt_any_mount_on_non_macos(self, plat):
        """No mount strategy should be attempted outside macOS."""
        with patch.object(sys, "platform", plat):
            with patch.object(force_mount, "execute_mount") as mock_mount:
                with pytest.raises(SystemExit):
                    force_mount.main()
        mock_mount.assert_not_called()

    @pytest.mark.parametrize("plat", NON_MACOS_PLATFORMS)
    def test_main_does_not_call_sudo_on_non_macos(self, plat):
        """os.execvp (sudo re-exec) must not be reached on non-macOS."""
        with patch.object(sys, "platform", plat):
            with patch("os.execvp") as mock_execvp:
                with pytest.raises(SystemExit):
                    force_mount.main()
        mock_execvp.assert_not_called()

    @pytest.mark.parametrize("plat", NON_MACOS_PLATFORMS)
    def test_main_produces_no_stdout_on_non_macos(self, plat, capsys):
        """Normal stdout (banners, progress) must be silent; message goes to stderr only."""
        with patch.object(sys, "platform", plat):
            with pytest.raises(SystemExit):
                force_mount.main()
        out = capsys.readouterr().out
        assert out == ""


# ===========================================================================
# get_active_mounts
# ===========================================================================

class TestGetActiveMounts:
    def test_parses_dev_entries(self):
        with patch("subprocess.run", return_value=_completed(stdout=MOUNT_OUTPUT_TEMPLATE)):
            result = force_mount.get_active_mounts()
        assert "/dev/disk1s1" in result
        assert "/dev/disk3s1" in result

    def test_ignores_non_dev_lines(self):
        mount_out = "map auto_home on /home (autofs, ...)\n/dev/disk1s1 on / (apfs)\n"
        with patch("subprocess.run", return_value=_completed(stdout=mount_out)):
            result = force_mount.get_active_mounts()
        assert "/dev/disk1s1" in result
        assert len(result) == 1

    def test_returns_empty_set_on_failure(self, capsys):
        with patch("subprocess.run", side_effect=Exception("boom")):
            result = force_mount.get_active_mounts()
        assert result == set()
        assert "WARNING" in capsys.readouterr().err

    def test_returns_empty_set_on_empty_output(self):
        with patch("subprocess.run", return_value=_completed(stdout="")):
            result = force_mount.get_active_mounts()
        assert result == set()


# ===========================================================================
# is_currently_mounted
# ===========================================================================

class TestIsCurrentlyMounted:
    def test_true_when_present(self):
        with patch.object(force_mount, "get_active_mounts", return_value={"/dev/disk4s1"}):
            assert force_mount.is_currently_mounted("disk4s1") is True

    def test_false_when_absent(self):
        with patch.object(force_mount, "get_active_mounts", return_value={"/dev/disk1s1"}):
            assert force_mount.is_currently_mounted("disk4s1") is False

    def test_always_calls_fresh_mount_query(self):
        """Must never rely on a cached set — each call must hit get_active_mounts."""
        with patch.object(force_mount, "get_active_mounts", return_value=set()) as mock_gam:
            force_mount.is_currently_mounted("disk4s1")
            force_mount.is_currently_mounted("disk4s1")
        assert mock_gam.call_count == 2


# ===========================================================================
# get_disk_data
# ===========================================================================

class TestGetDiskData:
    def test_returns_parsed_plist(self):
        raw = plistlib.dumps(DISKUTIL_PLIST_EXTERNAL_EXFAT)
        mock_result = MagicMock()
        mock_result.stdout = raw
        with patch("subprocess.run", return_value=mock_result):
            data = force_mount.get_disk_data()
        assert "AllDisksAndPartitions" in data

    def test_exits_on_diskutil_failure(self):
        with patch(
            "subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "diskutil"),
        ):
            with pytest.raises(SystemExit):
                force_mount.get_disk_data()


# ===========================================================================
# get_partition_fs_type
# ===========================================================================

class TestGetPartitionFsType:
    def test_returns_filesystem_type_lowercase(self):
        info = {"FilesystemType": "ExFAT", "Content": "Microsoft Basic Data"}
        mock_result = MagicMock()
        mock_result.stdout = plistlib.dumps(info)
        with patch("subprocess.run", return_value=mock_result):
            assert force_mount.get_partition_fs_type("disk4s1") == "exfat"

    def test_falls_back_to_content_when_no_filesystem_type(self):
        info = {"Content": "DOS_FAT_32"}
        mock_result = MagicMock()
        mock_result.stdout = plistlib.dumps(info)
        with patch("subprocess.run", return_value=mock_result):
            assert force_mount.get_partition_fs_type("disk4s1") == "dos_fat_32"

    def test_returns_empty_string_on_failure(self):
        with patch("subprocess.run", side_effect=Exception("boom")):
            assert force_mount.get_partition_fs_type("disk4s1") == ""

    def test_returns_empty_string_when_both_keys_missing(self):
        mock_result = MagicMock()
        mock_result.stdout = plistlib.dumps({"SomeOtherKey": "value"})
        with patch("subprocess.run", return_value=mock_result):
            assert force_mount.get_partition_fs_type("disk4s1") == ""


# ===========================================================================
# filter_target_partitions
# ===========================================================================

class TestFilterTargetPartitions:
    def _patch_mounts(self, mounted=None):
        return patch.object(
            force_mount, "get_active_mounts", return_value=set(mounted or [])
        )

    def test_picks_up_unmounted_external_exfat(self):
        with self._patch_mounts():
            targets = force_mount.filter_target_partitions(DISKUTIL_PLIST_EXTERNAL_EXFAT)
        assert targets == ["disk4s1"]

    def test_skips_internal_disks(self):
        with self._patch_mounts():
            targets = force_mount.filter_target_partitions(DISKUTIL_PLIST_INTERNAL)
        assert targets == []

    def test_skips_efi_partition(self):
        with self._patch_mounts():
            targets = force_mount.filter_target_partitions(DISKUTIL_PLIST_EFI)
        # disk4s1 = EFI (blocked), disk4s2 = Microsoft Basic Data (allowed)
        assert "disk4s1" not in targets
        assert "disk4s2" in targets

    def test_skips_apple_apfs(self):
        with self._patch_mounts():
            targets = force_mount.filter_target_partitions(DISKUTIL_PLIST_APFS)
        assert targets == []

    def test_skips_already_mounted_device(self, capsys):
        with self._patch_mounts(mounted=["/dev/disk4s1"]):
            targets = force_mount.filter_target_partitions(DISKUTIL_PLIST_EXTERNAL_EXFAT)
        assert targets == []
        assert "already mounted" in capsys.readouterr().out

    def test_multi_disk_multi_partition(self):
        # disk4s1=EFI(skip), disk4s2=MBD(ok), disk4s3=DOS_FAT(ok), disk5s1=exfat(ok)
        with self._patch_mounts():
            targets = force_mount.filter_target_partitions(DISKUTIL_PLIST_MULTI)
        assert "disk4s1" not in targets
        assert "disk4s2" in targets
        assert "disk4s3" in targets
        assert "disk5s1" in targets

    def test_empty_disk_data_returns_empty(self):
        with self._patch_mounts():
            targets = force_mount.filter_target_partitions({"AllDisksAndPartitions": []})
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
            targets = force_mount.filter_target_partitions(data)
        assert targets == []

    @pytest.mark.parametrize("content_type", [
        "Microsoft Basic Data",
        "Windows_NTFS",
        "DOS_FAT",
        "exfat",
        "ExFAT",
    ])
    def test_all_allowlisted_content_types_are_accepted(self, content_type):
        data = {
            "AllDisksAndPartitions": [
                {
                    "Internal": False,
                    "Partitions": [
                        {"DeviceIdentifier": "disk4s1", "Content": content_type}
                    ],
                }
            ]
        }
        with self._patch_mounts():
            targets = force_mount.filter_target_partitions(data)
        assert "disk4s1" in targets

    @pytest.mark.parametrize("content_type", [
        "Apple_APFS",
        "Apple_HFS",
        "Apple_Boot",
        "Apple_Recovery",
        "Apple_CoreStorage",
        "EFI",
    ])
    def test_all_blocklisted_content_types_are_rejected(self, content_type):
        data = {
            "AllDisksAndPartitions": [
                {
                    "Internal": False,
                    "Partitions": [
                        {"DeviceIdentifier": "disk4s1", "Content": content_type}
                    ],
                }
            ]
        }
        with self._patch_mounts():
            targets = force_mount.filter_target_partitions(data)
        assert targets == []


# ===========================================================================
# _run_diskutil_mount
# ===========================================================================

class TestRunDiskutilMount:
    def test_returns_true_on_success(self):
        with patch("subprocess.run", return_value=_completed(returncode=0)):
            assert force_mount._run_diskutil_mount("disk4s1", verbose=False) is True

    def test_returns_false_on_failure(self):
        with patch("subprocess.run", return_value=_completed(returncode=1)):
            assert force_mount._run_diskutil_mount("disk4s1", verbose=False) is False

    def test_prints_stderr_when_verbose(self, capsys):
        with patch("subprocess.run", return_value=_completed(returncode=1, stderr="oops")):
            force_mount._run_diskutil_mount("disk4s1", verbose=True)
        assert "oops" in capsys.readouterr().err

    def test_silent_on_stderr_when_not_verbose(self, capsys):
        with patch("subprocess.run", return_value=_completed(returncode=1, stderr="oops")):
            force_mount._run_diskutil_mount("disk4s1", verbose=False)
        assert capsys.readouterr().err == ""


# ===========================================================================
# _run_raw_mount
# ===========================================================================

class TestRunRawMount:
    def test_exfat_tried_first_for_unknown_fs(self, tmp_path):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd[0])
            return _completed(returncode=0)

        with patch("subprocess.run", side_effect=fake_run):
            with patch("pathlib.Path.mkdir"):
                force_mount._run_raw_mount("disk4s1", fs_type="", verbose=False)

        assert "mount_exfat" in calls[0]

    def test_msdos_tried_first_for_fat32(self, tmp_path):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd[0])
            return _completed(returncode=0)

        with patch("subprocess.run", side_effect=fake_run):
            with patch("pathlib.Path.mkdir"):
                force_mount._run_raw_mount("disk4s1", fs_type="msdos", verbose=False)

        assert "mount_msdos" in calls[0]

    def test_falls_back_to_second_binary(self):
        results = [_completed(returncode=1), _completed(returncode=0)]

        with patch("subprocess.run", side_effect=results):
            with patch("pathlib.Path.mkdir"):
                result = force_mount._run_raw_mount("disk4s1", fs_type="", verbose=False)

        assert result is True

    def test_returns_false_when_both_fail(self, tmp_path):
        with patch("subprocess.run", return_value=_completed(returncode=1)):
            with patch("pathlib.Path.mkdir"):
                with patch("pathlib.Path.rmdir") as mock_rmdir:
                    result = force_mount._run_raw_mount("disk4s1", fs_type="", verbose=False)

        assert result is False
        mock_rmdir.assert_called_once()

    def test_rmdir_not_called_on_success(self):
        with patch("subprocess.run", return_value=_completed(returncode=0)):
            with patch("pathlib.Path.mkdir"):
                with patch("pathlib.Path.rmdir") as mock_rmdir:
                    force_mount._run_raw_mount("disk4s1", fs_type="", verbose=False)

        mock_rmdir.assert_not_called()

    def test_verbose_stderr_printed_on_failure(self, capsys):
        with patch("subprocess.run", return_value=_completed(returncode=1, stderr="bad device")):
            with patch("pathlib.Path.mkdir"):
                with patch("pathlib.Path.rmdir"):
                    force_mount._run_raw_mount("disk4s1", fs_type="", verbose=True)

        assert "bad device" in capsys.readouterr().err


# ===========================================================================
# execute_mount
# ===========================================================================

class TestExecuteMount:
    def test_dry_run_returns_true_without_mounting(self):
        with patch.object(force_mount, "is_currently_mounted", return_value=False):
            with patch.object(force_mount, "get_partition_fs_type", return_value="exfat"):
                with patch.object(force_mount, "_run_diskutil_mount") as mock_du:
                    result = force_mount.execute_mount("disk4s1", dry_run=True)

        assert result is True
        mock_du.assert_not_called()

    def test_skips_if_mounted_since_scan(self, capsys):
        with patch.object(force_mount, "is_currently_mounted", return_value=True):
            result = force_mount.execute_mount("disk4s1")
        assert result is False
        assert "SKIPPED" in capsys.readouterr().out

    def test_succeeds_via_diskutil(self):
        mounted_sequence = [False, True]   # unmounted at check, mounted after diskutil

        with patch.object(force_mount, "is_currently_mounted", side_effect=mounted_sequence):
            with patch.object(force_mount, "get_partition_fs_type", return_value="exfat"):
                with patch.object(force_mount, "_run_diskutil_mount", return_value=True):
                    with patch.object(force_mount, "_find_mount_point", return_value="/Volumes/CARD"):
                        result = force_mount.execute_mount("disk4s1")

        assert result is True

    def test_falls_back_to_raw_mount_when_diskutil_fails(self):
        # Call sequence for is_currently_mounted:
        #   1. race-guard check → False (not yet mounted, proceed)
        #   2. post-raw-mount verify → True (raw mount succeeded)
        # Note: when _run_diskutil_mount returns False, its inner
        # is_currently_mounted check is never reached (short-circuit).
        mounted_sequence = [False, True]

        with patch.object(force_mount, "is_currently_mounted", side_effect=mounted_sequence):
            with patch.object(force_mount, "get_partition_fs_type", return_value=""):
                with patch.object(force_mount, "_run_diskutil_mount", return_value=False):
                    with patch.object(force_mount, "_run_raw_mount", return_value=True):
                        result = force_mount.execute_mount("disk4s1")

        assert result is True

    def test_returns_false_when_all_strategies_fail(self, capsys):
        with patch.object(force_mount, "is_currently_mounted", return_value=False):
            with patch.object(force_mount, "get_partition_fs_type", return_value=""):
                with patch.object(force_mount, "_run_diskutil_mount", return_value=False):
                    with patch.object(force_mount, "_run_raw_mount", return_value=False):
                        result = force_mount.execute_mount("disk4s1")

        assert result is False
        assert "CRITICAL ERROR" in capsys.readouterr().out


# ===========================================================================
# _find_mount_point
# ===========================================================================

class TestFindMountPoint:
    def test_extracts_correct_mount_point(self):
        mount_out = "/dev/disk4s1 on /Volumes/CARD (exfat, local)\n"
        with patch("subprocess.run", return_value=_completed(stdout=mount_out)):
            result = force_mount._find_mount_point("disk4s1")
        assert result == "/Volumes/CARD"

    def test_returns_empty_string_when_not_found(self):
        with patch("subprocess.run", return_value=_completed(stdout=MOUNT_OUTPUT_TEMPLATE)):
            result = force_mount._find_mount_point("disk9s9")
        assert result == ""

    def test_returns_empty_string_on_exception(self):
        with patch("subprocess.run", side_effect=Exception("boom")):
            result = force_mount._find_mount_point("disk4s1")
        assert result == ""

    def test_handles_mount_point_with_spaces(self):
        mount_out = "/dev/disk4s1 on /Volumes/My Card (exfat, local)\n"
        with patch("subprocess.run", return_value=_completed(stdout=mount_out)):
            result = force_mount._find_mount_point("disk4s1")
        assert result == "/Volumes/My Card"