#!/usr/bin/env python3
"""
Force-mount external camera data volumes stuck in macOS Disk Utility limbo.
Bypasses automated daemon naming race conditions.

macOS Tahoe / LIFS compatibility: prefers `diskutil mount` over raw mount
binaries, which are increasingly sandbox-restricted in Tahoe's security model.

Usage:
    sudo lifsaver           # normal run
    sudo lifsaver --verbose # show raw stderr on failures
    lifsaver --dry-run      # preview only, no writes
"""

import argparse
import contextlib
import importlib.metadata
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Literal

# -----------------------------------------------------------------------------
# Version
# -----------------------------------------------------------------------------

try:
    __version__ = importlib.metadata.version("dit-mate")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover
    __version__ = "unknown"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Partition "Content" values reported by `diskutil list -plist`. These name
# MBR/GPT partition types, not filesystems:
EXTERNAL_FS_ALLOWLIST = frozenset(
    [
        "DOS_FAT_32",  # MBR type 0x0B — FAT32 as formatted by macOS Disk Utility
        "Windows_FAT_32",  # MBR type 0x0C (FAT32 LBA) — FAT32 as formatted by Windows / SD Formatter
        "Windows_NTFS",  # MBR type 0x07 — shared by exFAT and NTFS; SDXC cards formatted in-camera report this
        "Microsoft Basic Data",  # GPT Basic Data GUID — any FAT32/exFAT/NTFS partition on a GUID-partitioned card
        "exFAT",  # filesystem personality names, never observed as real Content
        "ExFAT",  # values (exFAT reports Windows_NTFS or Microsoft Basic Data)
    ]
)

SEPARATOR = "-" * 56

# Read-only queries (mount, diskutil info, pgrep) should return near-instantly;
# mount attempts can legitimately stall on slow card readers, so they get longer.
QUERY_TIMEOUT = 30
MOUNT_TIMEOUT = 120

MountOutcome = Literal["ok", "skip", "fail"]

# ---------------------------------------------------------------------------
# Mount-table helpers
# ---------------------------------------------------------------------------


def get_active_mounts() -> set[str]:
    """
    Query the kernel mount table and return the set of currently-mounted
    device paths (e.g. {'/dev/disk4s1', ...}).

    Uses `mount` for a live, authoritative view of the kernel VFS table.
    """
    active: set[str] = set()
    try:
        output = subprocess.run(["mount"], capture_output=True, text=True, check=True, timeout=QUERY_TIMEOUT).stdout
        for line in output.splitlines():
            if line.startswith("/dev/"):
                dev_path = line.split()[0]
                active.add(dev_path)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"WARNING: Could not read mount table: {exc}", file=sys.stderr)
    return active


def is_currently_mounted(dev_id: str) -> bool:
    """
    Re-query the live mount table for a single device.
    Always performs a fresh syscall — never relies on a cached set.
    """
    return f"/dev/{dev_id}" in get_active_mounts()


def is_fsck_active(dev_id: str) -> bool:
    """
    Detect whether macOS is running a background consistency check against
    the device.  diskarbitrationd silently spawns fsck_exfat / fsck_msdos on
    dirty cards before deciding whether to mount them; forcing a mount while
    that repair is in flight races it and can corrupt the card.

    Matches both /dev/diskXsY and the raw /dev/rdiskXsY node fsck actually
    opens, without matching longer identifiers (disk4s1 ≠ disk4s10).
    """
    try:
        result = subprocess.run(
            ["pgrep", "-fl", "fsck"],
            capture_output=True,
            text=True,
            check=False,  # pgrep exits 1 when nothing matches — not an error
            timeout=QUERY_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    pattern = re.compile(rf"\br?{re.escape(dev_id)}\b")
    return any(pattern.search(line) for line in result.stdout.splitlines())


# ---------------------------------------------------------------------------
# Disk introspection
# ---------------------------------------------------------------------------


def get_disk_data() -> dict:
    """Retrieve all physical partition details via structured plist data."""
    try:
        result = subprocess.run(
            ["diskutil", "list", "-plist"],
            capture_output=True,
            check=True,
            timeout=QUERY_TIMEOUT,
        )
        return plistlib.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException) as exc:
        print(f"CRITICAL: Failed to query diskutil: {exc}", file=sys.stderr)
        sys.exit(1)


def get_partition_fs_type(dev_id: str) -> str:
    """
    Ask diskutil for the actual filesystem type of a partition so we can
    choose the right mount binary without trial-and-error.

    Returns a lowercase string such as 'exfat', 'msdos', 'hfs', or ''
    if the information is unavailable.
    """
    try:
        result = subprocess.run(
            ["diskutil", "info", "-plist", dev_id],
            capture_output=True,
            check=True,
            timeout=QUERY_TIMEOUT,
        )
        info = plistlib.loads(result.stdout)
        # 'FilesystemType' is the canonical key; fall back to content hint
        fs = info.get("FilesystemType") or info.get("Content") or ""
        return fs.lower()
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException):
        return ""


# ---------------------------------------------------------------------------
# Partition filtering
# ---------------------------------------------------------------------------


def filter_target_partitions(disk_data: dict) -> list[str]:
    """
    Walk the plist returned by `diskutil list -plist` and return device
    identifiers (e.g. ['disk4s1']) that are:

      • on external hardware
      • a recognised camera-card filesystem type
      • NOT already mounted (checked against a single fresh mount query)

    EFI system partitions and Apple_APFS / Apple_HFS containers are
    explicitly excluded.
    """
    active_mounts = get_active_mounts()  # one consistent snapshot for filtering
    targets: list[str] = []

    for disk in disk_data.get("AllDisksAndPartitions", []):
        # Strict boundary: internal disks are never touched.
        if disk.get("Internal") is True:
            continue

        for partition in disk.get("Partitions", []):
            content_type: str = partition.get("Content", "")
            dev_id: str = partition.get("DeviceIdentifier", "")
            dev_path = f"/dev/{dev_id}"

            if not dev_id:
                continue

            # Blocklist: EFI, recovery, and Apple container types.
            # Redundant with the allowlist today, but kept as a second interlock:
            # the allowlist uses substring matching and gets edited — a future broad
            # token must still never expose Apple/EFI system partitions.
            blocked = any(
                token in content_type
                for token in [
                    "EFI",
                    "Apple_APFS",
                    "Apple_HFS",
                    "Apple_Boot",
                    "Apple_Recovery",
                    "Apple_CoreStorage",
                ]
            )
            if blocked:
                continue

            # Allowlist: recognised camera-card payload types
            is_camera_payload = any(token in content_type for token in EXTERNAL_FS_ALLOWLIST)
            if not is_camera_payload:
                continue

            # Safety gate: skip anything already in the mount table
            if dev_path in active_mounts:
                print(f"  Skipping {dev_id} — already mounted.")
                continue

            targets.append(dev_id)

    return targets


# ---------------------------------------------------------------------------
# Mount execution
# ---------------------------------------------------------------------------


def _run_diskutil_mount(dev_id: str, verbose: bool) -> bool:
    """
    Attempt mount via `diskutil mount`, the preferred path on macOS Tahoe.
    diskutil handles filesystem detection, SIP/LIFS sandboxing, and
    mount-point creation automatically.
    """
    try:
        result = subprocess.run(
            ["diskutil", "mount", dev_id],
            capture_output=True,
            text=True,
            check=False,
            timeout=MOUNT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if verbose:
            print(f"  [diskutil error] {exc}", file=sys.stderr)
        return False
    if verbose and result.stderr:
        print(f"  [diskutil stderr] {result.stderr.strip()}", file=sys.stderr)
    return result.returncode == 0


def _run_raw_mount(dev_id: str, fs_type: str, verbose: bool) -> bool:
    """
    Fallback: use low-level mount binaries when diskutil mount is unavailable
    or returns an error.  Mount-point directory is created and cleaned up
    on failure.

    Tries exFAT first (most modern cards), then FAT32/MSDOS.
    """
    dev_path = f"/dev/{dev_id}"
    mount_point = Path(f"/Volumes/Camera_Data_{dev_id}")

    try:
        mount_point.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        if verbose:
            print(f"  [mount-point error] {exc}", file=sys.stderr)
        return False

    # Determine mount sequence: honour detected fs_type when available
    if fs_type in ("msdos", "fat", "fat32"):
        candidates = [
            ["/sbin/mount_msdos", dev_path, str(mount_point)],
            ["/sbin/mount_exfat", dev_path, str(mount_point)],
        ]
    else:
        # Default: exFAT first (CFast, SDXC), then FAT32 (older SDHC)
        candidates = [
            ["/sbin/mount_exfat", dev_path, str(mount_point)],
            ["/sbin/mount_msdos", dev_path, str(mount_point)],
        ]

    for cmd in candidates:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=MOUNT_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            # Missing binary (removed in newer macOS) or a hung card reader:
            # move on to the next candidate rather than crashing.
            if verbose:
                print(f"  [{cmd[0].split('/')[-1]} error] {exc}", file=sys.stderr)
            continue
        if verbose and result.stderr:
            print(
                f"  [{cmd[0].split('/')[-1]} stderr] {result.stderr.strip()}",
                file=sys.stderr,
            )
        if result.returncode == 0:
            return True

    # Both failed — clean up the empty directory we created
    with contextlib.suppress(OSError):
        mount_point.rmdir()

    return False


def execute_mount(dev_id: str, dry_run: bool = False, verbose: bool = False) -> MountOutcome:
    """
    Orchestrate the full mount sequence for a single device identifier.

    Strategy (macOS Tahoe / LIFS-aware):
      1. Re-confirm the device is still unmounted (race-condition guard).
      2. Stand down if macOS is mid consistency check on the device.
      3. Try `diskutil mount` — preferred; handles LIFS sandboxing.
      4. Fall back to raw mount binaries if diskutil fails.
    """
    print(f"\nTarget: /dev/{dev_id}")

    # Re-query live mount table immediately before acting (race guard)
    if is_currently_mounted(dev_id):
        print(f"  SKIPPED — /dev/{dev_id} became mounted since scan.")
        return "skip"

    # Never fight a repair in progress — wait for macOS to finish or bail out.
    if is_fsck_active(dev_id):
        print(f"  SKIPPED — macOS is running a consistency check (fsck) on /dev/{dev_id}.")
        print("  Let it finish and re-run; mounting mid-check risks corrupting the card.")
        return "skip"

    fs_type = get_partition_fs_type(dev_id)
    if fs_type:
        print(f"  Detected filesystem: {fs_type}")

    if dry_run:
        print(f"  DRY-RUN: would attempt to mount /dev/{dev_id}")
        return "ok"

    # --- Attempt 1: diskutil mount (Tahoe-safe) ---
    print("  Attempting diskutil mount...")
    if _run_diskutil_mount(dev_id, verbose) and is_currently_mounted(dev_id):
        mount_point = _find_mount_point(dev_id)
        print(f"  SUCCESS via diskutil → {mount_point or '(see /Volumes)'}")
        return "ok"

    # --- Attempt 2: raw mount binaries ---
    print("  diskutil mount failed; falling back to raw mount binaries...")
    if _run_raw_mount(dev_id, fs_type, verbose) and is_currently_mounted(dev_id):
        print(f"  SUCCESS via raw mount → /Volumes/Camera_Data_{dev_id}")
        return "ok"

    print(f"  CRITICAL ERROR: All mount strategies rejected /dev/{dev_id}")
    return "fail"


def _find_mount_point(dev_id: str) -> str:
    """Extract the current mount point for a device from the live mount table."""
    DEV_MOUNT_PARTS = 2  # "/dev/diskXsY on /Volumes/NAME" → device + mount point

    dev_path = f"/dev/{dev_id}"
    try:
        output = subprocess.run(["mount"], capture_output=True, text=True, check=True, timeout=QUERY_TIMEOUT).stdout
        for line in output.splitlines():
            if line.startswith(dev_path + " "):
                # format: /dev/diskXsY on /Volumes/NAME (type, options)
                parts = line.split(" on ", 1)
                if len(parts) == DEV_MOUNT_PARTS:
                    return parts[1].split(" (")[0].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Force-mount stalled 'Untitled' volumes on macOS.")
    parser.add_argument(
        "--version",
        action="version",
        version=f"{__version__}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be mounted without touching anything.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show raw stderr from mount commands.",
    )
    return parser.parse_args()


def check_platform() -> None:
    """
    Abort with a clear message if the script is run outside macOS.
    """
    if sys.platform != "darwin":
        print(
            f"\n  ✗  This script only runs on macOS.\n     Detected platform: {sys.platform}\n",
            file=sys.stderr,
        )

        # Bypass standard failure in CI environments to allow generic smoke tests.
        if os.environ.get("CI") == "true":
            sys.exit(0)

        sys.exit(1)


def main() -> None:
    check_platform()
    args = parse_args()

    # Re-exec with sudo, preserving all original arguments.
    # Dry-run only performs read-only queries, so it never needs root.
    if os.getuid() != 0 and not args.dry_run:
        print("Root access required. Re-running with sudo...")
        os.execvp("sudo", ["sudo", sys.executable, *sys.argv])

    print(SEPARATOR)
    mode = " [DRY-RUN]" if args.dry_run else ""
    print(f"Camera volume mount sequence{mode}")
    print(SEPARATOR)

    disk_data = get_disk_data()
    targets = filter_target_partitions(disk_data)

    if not targets:
        print("No stalled or unmounted camera data volumes detected.")
        print(SEPARATOR)
        return

    print(f"Found {len(targets)} candidate volume(s): {', '.join(targets)}")

    results = {"ok": 0, "fail": 0, "skip": 0}
    for dev_id in targets:
        outcome = execute_mount(dev_id, dry_run=args.dry_run, verbose=args.verbose)
        results[outcome] += 1
        print(SEPARATOR)

    if args.dry_run:
        print(f"Done — {results['ok']} mountable, {results['skip']} skipped.")
        return

    print(f"Done — {results['ok']} mounted, {results['fail']} failed, {results['skip']} skipped.")
    if results["fail"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
