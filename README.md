# dit-mate

`dit-mate` is a toolkit for Digital Imaging Technicians and Media Managers, fully cross-platform. It contains the following tools:

* `basicmeta` lists the essential metadata that needs to be consistent across all cameras in a given shooting day: frame rate, resolution and recorded date. It integrates [Media-Info](https://github.com/mediaarea/mediainfo) and [ExifTool](https://github.com/exiftool/exiftool) to support all professional camera acquisition formats (MXF, MOV, MP4, R3D, and BWF WAV audio), as well as some extra video containers (MKV, AVI, M4V, MTS, FLV, WebM).

* `mkday` creates a user-defined folder structure in the volumes provided, a time-saver at the beginning of each shooting day.

* `lifsaver` addresses a macOS bug on LIFS (Live Image File System) which prevents multiple cards from mounting when they have the name "Untitled". Run it to force mount any card that is found.

* `mrl` takes a directory with camera rolls and copies to the clipboard the values you need to paste in your Master Rushes Log: first and last clip name, clip count, size and duration. It requires [FFmpeg](https://github.com/ffmpeg/ffmpeg), and it handles clips split across multiple files correctly (like RED .R3D and GoPros).

* `rename-roll` uses an editable dictionary (.TSV) to rename camera rolls in multiple directories. Useful in combination with ShotPut Pro when roll names need to have longer names that the volume admits

### 🚀 Installation

1. Install prerequisites ([Media-Info](https://mediaarea.net/en/MediaInfo/Download), [ExifTool](https://exiftool.org/install.html), [FFmpeg](https://www.ffmpeg.org/download.html)) and [uv](https://docs.astral.sh/uv/getting-started/installation/) from the official installers, or use a package manager:

* macOS: `brew install media-info exiftool ffmpeg uv`
* Windows: `winget install MediaArea.MediaInfo OliverBetz.ExifTool Gyan.FFmpeg astral-sh.uv`
* Linux (Debian): `apt-get install mediainfo libimage-exiftool-perl ffmpeg uv`
<!--
* Linux (RHEL): `yum install mediainfo perl-Image-ExifTool ffmpeg uv`
* Linux (SUSE): `zypper install mediainfo exiftool ffmpeg python-uv`
* Linux (Arch): `pacman -S mediainfo exiftool ffmpeg uv`
-->

2. Install the toolkit:

```
uv tool install dit-mate
```

3. Test any of the tools (if the command is not recognised try `uv tool update-shell` and restart Terminal):

```
basicmeta --version
```

### 📖 Examples

Check the essential metadata of multiple camera rolls:

```bash
basicmeta "path/to/rushes/"
basicmeta									# scans the current directory
```
Make the folder structure for shooting day 1, using the preset "example", on two backup drives:
```bash
mkday -p example_preset -d 1 "path/to/drive1" "path/to/drive2"
```

Copy the values you need for your Master Rushes Log:

```bash
mrl "path/to/camera/roll"					# copy default values
mrl "path/to/camera/roll" -cs				# copy only clip count and size (in that order)
mrl "path/to/rushes/"						# auto-detects multiple rolls
mrl											# scans the current directory
```

Run any tool with `--help` to see the full list of options.


### 🧪 Feedback & Contributing

If this tool fails to parse metadata from your specific camera files, or if you have ideas for improvement, please fork the repository and submit a pull request, or open an issue with a sample of the problematic metadata output. Help me make this tool more robust for the DIT community!