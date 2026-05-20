#!/usr/bin/env python3
"""
Force-mount external camera data volumes stuck in macOS Disk Utility limbo.
Bypasses automated daemon naming race conditions for ShotPut Pro queues.

macOS Tahoe / LIFS compatibility: prefers `diskutil mount` over raw mount
binaries, which are increasingly sandbox-restricted in Tahoe's security model.

Usage:
    sudo lifsaver           # normal run
    sudo lifsaver --dry-run # preview only, no writes
    sudo lifsaver --verbose # show raw stderr on failures
"""

import argparse
import importlib.metadata
import os
import plistlib
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Version is imported from dit-mate
__version__ = importlib.metadata.version("dit-mate")

EXTERNAL_FS_ALLOWLIST = frozenset(
    ["Microsoft Basic Data", "Windows_NTFS", "DOS_FAT", "exfat", "ExFAT"]
)

SEPARATOR = "-" * 56


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
        output = subprocess.run(
            ["mount"], capture_output=True, text=True, check=True
        ).stdout
        for line in output.splitlines():
            if line.startswith("/dev/"):
                dev_path = line.split()[0]
                active.add(dev_path)
    except Exception as exc:
        print(f"WARNING: Could not read mount table: {exc}", file=sys.stderr)
    return active


def is_currently_mounted(dev_id: str) -> bool:
    """
    Re-query the live mount table for a single device.
    Always performs a fresh syscall — never relies on a cached set.
    """
    return f"/dev/{dev_id}" in get_active_mounts()


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
        )
        return plistlib.loads(result.stdout)
    except subprocess.CalledProcessError as exc:
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
        )
        info = plistlib.loads(result.stdout)
        # 'FilesystemType' is the canonical key; fall back to content hint
        fs = info.get("FilesystemType") or info.get("Content") or ""
        return fs.lower()
    except Exception:
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
    active_mounts = get_active_mounts()   # one consistent snapshot for filtering
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

            # Blocklist: EFI, recovery, and Apple container types
            blocked = any(
                token in content_type
                for token in ["EFI", "Apple_APFS", "Apple_HFS", "Apple_Boot",
                              "Apple_Recovery", "Apple_CoreStorage"]
            )
            if blocked:
                continue

            # Allowlist: recognised camera-card payload types
            is_camera_payload = any(
                token in content_type for token in EXTERNAL_FS_ALLOWLIST
            )
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
    result = subprocess.run(
        ["diskutil", "mount", dev_id],
        capture_output=True,
        text=True,
    )
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

    mount_point.mkdir(parents=True, exist_ok=True)

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
        result = subprocess.run(cmd, capture_output=True, text=True)
        if verbose and result.stderr:
            print(f"  [{cmd[0].split('/')[-1]} stderr] {result.stderr.strip()}",
                  file=sys.stderr)
        if result.returncode == 0:
            return True

    # Both failed — clean up the empty directory we created
    try:
        mount_point.rmdir()
    except OSError:
        pass

    return False


def execute_mount(dev_id: str, dry_run: bool = False, verbose: bool = False) -> bool:
    """
    Orchestrate the full mount sequence for a single device identifier.

    Strategy (macOS Tahoe / LIFS-aware):
      1. Re-confirm the device is still unmounted (race-condition guard).
      2. Try `diskutil mount` — preferred; handles LIFS sandboxing.
      3. Fall back to raw mount binaries if diskutil fails.
    """
    print(f"\nTarget: /dev/{dev_id}")

    # Re-query live mount table immediately before acting (race guard)
    if is_currently_mounted(dev_id):
        print(f"  SKIPPED — /dev/{dev_id} became mounted since scan.")
        return False

    fs_type = get_partition_fs_type(dev_id)
    if fs_type:
        print(f"  Detected filesystem: {fs_type}")

    if dry_run:
        print(f"  DRY-RUN: would attempt to mount /dev/{dev_id}")
        return True

    # --- Attempt 1: diskutil mount (Tahoe-safe) ---
    print(f"  Attempting diskutil mount...")
    if _run_diskutil_mount(dev_id, verbose):
        if is_currently_mounted(dev_id):
            mount_point = _find_mount_point(dev_id)
            print(f"  SUCCESS via diskutil → {mount_point or '(see /Volumes)'}")
            return True

    # --- Attempt 2: raw mount binaries ---
    print(f"  diskutil mount failed; falling back to raw mount binaries...")
    if _run_raw_mount(dev_id, fs_type, verbose):
        if is_currently_mounted(dev_id):
            print(f"  SUCCESS via raw mount → /Volumes/Camera_Data_{dev_id}")
            return True

    print(f"  CRITICAL ERROR: All mount strategies rejected /dev/{dev_id}")
    return False


def _find_mount_point(dev_id: str) -> str:
    """Extract the current mount point for a device from the live mount table."""
    dev_path = f"/dev/{dev_id}"
    try:
        output = subprocess.run(
            ["mount"], capture_output=True, text=True, check=True
        ).stdout
        for line in output.splitlines():
            if line.startswith(dev_path + " "):
                # format: /dev/diskXsY on /Volumes/NAME (type, options)
                parts = line.split(" on ", 1)
                if len(parts) == 2:
                    return parts[1].split(" (")[0].strip()
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Force-mount stalled 'Untitled' volumes on macOS."
    )
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

    Every tool used here (diskutil, mount_exfat, mount_msdos, Disk
    Arbitration, /Volumes, plist output) is macOS-specific.  On Linux
    use udisksctl/mount; on Windows use mountvol or Disk Management.
    """
    if sys.platform != "darwin":
        platform_hints = {
            "linux":  "On Linux, try:  udisksctl mount -b /dev/<device>",
            "win32":  "On Windows, use Disk Management or: mountvol <drive>: /L",
            "cygwin": "On Windows (Cygwin), use Disk Management or mountvol.",
        }
        hint = platform_hints.get(sys.platform, "")
        print(
            f"\n  ✗  This script only runs on macOS.\n"
            f"     Detected platform: {sys.platform}\n"
            f"\n"
            f"     The tools it relies on — diskutil, mount_exfat, mount_msdos,\n"
            f"     Disk Arbitration, /Volumes, and plist kernel output — do not\n"
            f"     exist on other operating systems.\n"
            + (f"\n     {hint}\n" if hint else ""),
            file=sys.stderr,
        )
        sys.exit(1)


def main() -> None:
    check_platform()
    args = parse_args()

    # Re-exec with sudo, preserving all original arguments
    if os.getuid() != 0:
        print("Root access required. Re-running with sudo...")
        os.execvp("sudo", ["sudo", sys.executable] + sys.argv)

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
        if outcome:
            results["ok"] += 1
        else:
            results["fail"] += 1
        print(SEPARATOR)

    print(
        f"Done — {results['ok']} mounted, "
        f"{results['fail']} failed, "
        f"{results['skip']} skipped."
    )


if __name__ == "__main__":
    main()