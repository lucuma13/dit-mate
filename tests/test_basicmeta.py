"""
Tests for basicmeta.
"""

import subprocess
import sys
from pathlib import Path

import dit_mate.basicmeta as bm
from dit_mate._internal.binaries import stream_lines

# ---------------------------------------------------------------------------
# stream_lines (binaries)
# ---------------------------------------------------------------------------


def test_stream_lines_yields_stdout_lines():
    lines = list(stream_lines([sys.executable, "-c", "print('a'); print('b')"]))
    assert lines == ["a\n", "b\n"]


def test_stream_lines_missing_binary_yields_nothing():
    assert list(stream_lines(["definitely-not-a-real-binary-xyz"])) == []


def test_stream_lines_terminates_child_on_early_exit():
    # An infinite printer: if the child weren't terminated, close() would
    # hang in proc.wait(). Surviving this test quickly is the assertion.
    code = "import sys\nwhile True: print('x'); sys.stdout.flush()"
    gen = stream_lines([sys.executable, "-c", code])
    assert next(gen) == "x\n"
    gen.close()


def test_stream_lines_never_raises_on_nonzero_exit():
    lines = list(stream_lines([sys.executable, "-c", "print('out'); raise SystemExit(3)"]))
    assert lines == ["out\n"]


# ---------------------------------------------------------------------------
# _stream_exiftool_records
# ---------------------------------------------------------------------------


def _patch_stream(monkeypatch, lines):
    monkeypatch.setattr(bm, "stream_lines", lambda cmd: iter(lines))


def test_records_split_on_headers(monkeypatch):
    _patch_stream(
        monkeypatch,
        [
            "======== /x/A.R3D\n",
            "FrameRate: 23.976\n",
            "DateTimeOriginal: 2024:06:15 10:30:00\n",
            "======== /x/B.R3D\n",
            "FrameRate: 25\n",
        ],
    )
    recs = list(bm._stream_exiftool_records(["exiftool"]))
    assert recs == [
        {"_file": "/x/A.R3D", "FrameRate": "23.976", "DateTimeOriginal": "2024:06:15 10:30:00"},
        {"_file": "/x/B.R3D", "FrameRate": "25"},
    ]


def test_records_single_file_has_no_file_key(monkeypatch):
    _patch_stream(monkeypatch, ["FrameRate: 24\n", "FileName: A.R3D\n"])
    recs = list(bm._stream_exiftool_records(["exiftool"]))
    assert recs == [{"FrameRate": "24", "FileName": "A.R3D"}]


def test_records_values_may_contain_colons(monkeypatch):
    _patch_stream(monkeypatch, ["======== /x/a.wav\n", "DateTimeOriginal: 2024:06:15 10:30:00\n"])
    (rec,) = bm._stream_exiftool_records(["exiftool"])
    assert rec["DateTimeOriginal"] == "2024:06:15 10:30:00"


# ---------------------------------------------------------------------------
# _stream_exiftool_rows via stream_r3d / stream_wav
# ---------------------------------------------------------------------------


def test_rows_come_out_in_input_order_with_placeholders(monkeypatch):
    paths = [Path("/x/A.R3D"), Path("/x/B.R3D"), Path("/x/C.R3D")]
    # B is unreadable: exiftool emitted no block for it.
    _patch_stream(
        monkeypatch,
        [
            "======== /x/A.R3D\n",
            "FrameRate: 23.976\n",
            "FileName: A.R3D\n",
            "======== /x/C.R3D\n",
            "FrameRate: 25\n",
            "FileName: C.R3D\n",
        ],
    )
    rows = list(bm.stream_r3d(paths))
    assert [p.name for p, _ in rows] == ["A.R3D", "B.R3D", "C.R3D"]
    assert rows[0][1][0] == "23.976"
    assert rows[1][1] == ("", "", "", "", "B.R3D")  # placeholder, in position
    assert rows[2][1][0] == "25"


def test_rows_single_file_matches_headerless_record(monkeypatch):
    _patch_stream(monkeypatch, ["FrameRate: 24\n", "ImageWidth: 4096\n", "ImageHeight: 2160\n"])
    (row,) = bm.stream_r3d([Path("/x/A.R3D")])
    assert row == (Path("/x/A.R3D"), ("24", "4096 x 2160", "", "", "A.R3D"))


def test_rows_missing_tool_yields_all_placeholders(monkeypatch):
    _patch_stream(monkeypatch, [])
    rows = list(bm.stream_wav([Path("/x/a.wav"), Path("/x/b.wav")]))
    assert rows == [
        (Path("/x/a.wav"), ("", "Audio", "", "", "a.wav")),
        (Path("/x/b.wav"), ("", "Audio", "", "", "b.wav")),
    ]


def test_rows_stream_before_output_is_exhausted(monkeypatch):
    """A file's row must be yielded as soon as the next header arrives."""
    paths = [Path("/x/A.R3D"), Path("/x/B.R3D")]
    lines = [
        "======== /x/A.R3D\n",
        "FrameRate: 24\n",
        "======== /x/B.R3D\n",
        "FrameRate: 25\n",
    ]
    consumed: list[str] = []

    def fake_stream(cmd):
        for line in lines:
            consumed.append(line)
            yield line

    monkeypatch.setattr(bm, "stream_lines", fake_stream)
    gen = bm.stream_r3d(paths)
    p, row = next(gen)
    assert p.name == "A.R3D"
    assert row[0] == "24"
    # A's row arrived after reading only up to B's header — B's tag lines
    # were not consumed from the pipe yet.
    assert consumed == lines[:3]
    assert [pp.name for pp, _ in gen] == ["B.R3D"]


def test_wav_rows_probe_tag_aliases_in_order(monkeypatch):
    _patch_stream(
        monkeypatch,
        [
            "======== /x/a.wav\n",
            "VideoFrameRate: 29.97\n",
            "BwfxmlSpeedTimecodeRate: 23.976\n",  # more specific: must win
            "DateCreated: 2024:06:15\n",
            "======== /x/b.wav\n",
            "Speed: 25\n",
            "DateTimeOriginal: 0000:00:00 00:00:00\n",  # placeholder: skipped
            "DateCreated: 2024-06-16\n",
        ],
    )
    rows = dict(bm.stream_wav([Path("/x/a.wav"), Path("/x/b.wav")]))
    assert rows[Path("/x/a.wav")] == ("23.976", "Audio", "2024-06-15", "", "a.wav")
    assert rows[Path("/x/b.wav")] == ("25", "Audio", "2024-06-16", "", "b.wav")


def test_process_batch_is_a_true_generator(monkeypatch):
    """_process_batch must not pre-compute all lines before yielding."""
    paths = [Path("/x/A.R3D"), Path("/x/B.R3D")]
    lines = [
        "======== /x/A.R3D\n",
        "FrameRate: 24\n",
        "======== /x/B.R3D\n",
        "FrameRate: 25\n",
    ]
    consumed: list[str] = []

    def fake_stream(cmd):
        for line in lines:
            consumed.append(line)
            yield line

    monkeypatch.setattr(bm, "stream_lines", fake_stream)
    gen = bm._process_batch([], paths, [], ["fps"])
    line, raw = next(gen)
    assert "24" in line
    assert raw[0] == "24"
    assert consumed == lines[:3]  # B not fully read yet


def test_stream_child_is_terminated_when_consumer_stops(monkeypatch):
    """Abandoning the row generator must not leave an exiftool child behind."""
    procs: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def tracking_popen(*args, **kwargs):
        code = "import sys\nwhile True: print('======== /x/A.R3D'); sys.stdout.flush()"
        proc = real_popen([sys.executable, "-c", code], **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", tracking_popen)
    gen = bm.stream_r3d([Path("/x/A.R3D"), Path("/x/B.R3D")])
    next(gen)
    gen.close()
    assert procs[0].poll() is not None  # reaped, not still running
