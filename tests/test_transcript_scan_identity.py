import concurrent.futures
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_scan_cache_binds_path_and_survives_concurrent_writers(tmp_path):
    source = (ROOT / "scripts" / "post-tool-hook.sh").read_text(encoding="utf-8")
    start = source.index("_pt_scan_lines() {")
    end = source.index("\nfi\n# CURRENT_LINES is 0 here", start)
    script = tmp_path / "scan.sh"
    script.write_text(
        'set -eu\nTRANSCRIPT="$1"\nREMEMBER_DIR="$2"\n'
        + source[start:end]
        + '\nprintf "%s\\n" "$CURRENT_LINES"\n',
        encoding="utf-8",
    )
    memory = tmp_path / "memory"
    (memory / "tmp").mkdir(parents=True)
    first = tmp_path / "a" / "same.jsonl"
    second = tmp_path / "b" / "same.jsonl"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"abc\ndef\n")
    second.write_bytes(b"abcdefg\nz\n")
    sidecar = memory / "tmp" / "transcript-scan.same"

    bash = str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe") if os.name == "nt" else "bash"

    def scan(path):
        proc = subprocess.run(
            [bash, str(script), str(path), str(memory)],
            capture_output=True,
            text=True,
            check=True,
            env=os.environ.copy(),
        )
        return int(proc.stdout.strip())

    assert scan(first) == 2
    assert scan(second) == 2
    sidecar.write_text("8 2\n", encoding="utf-8")
    assert scan(second) == 2
    first.write_bytes(b"abc\ndef\n123\n")
    second.write_bytes(b"abcdefg\nz\nq\nr\n")
    paths = [first, second] * 10
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        observed = list(pool.map(scan, paths))
    assert observed == [3, 4] * 10


def test_scan_cache_rename_failure_preserves_previous_entry(tmp_path):
    source = (ROOT / "scripts" / "post-tool-hook.sh").read_text(encoding="utf-8")
    start = source.index("_pt_scan_lines() {")
    end = source.index("\nfi\n# CURRENT_LINES is 0 here", start)
    script = tmp_path / "scan.sh"
    script.write_text(
        'set -eu\nTRANSCRIPT="$1"\nREMEMBER_DIR="$2"\n'
        + 'mv() { return 1; }\n'
        + source[start:end]
        + '\nprintf "%s\\n" "$CURRENT_LINES"\n',
        encoding="utf-8",
    )
    memory = tmp_path / "memory"
    (memory / "tmp").mkdir(parents=True)
    transcript = tmp_path / "same.jsonl"
    transcript.write_bytes(b"one\ntwo\n")
    sidecar = memory / "tmp" / "transcript-scan.same"
    old_entry = "0 0\nold-path\n"
    sidecar.write_text(old_entry, encoding="utf-8")
    bash = str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe") if os.name == "nt" else "bash"
    proc = subprocess.run(
        [bash, str(script), str(transcript), str(memory)],
        capture_output=True,
        text=True,
        check=True,
        env=os.environ.copy(),
    )
    assert proc.stdout.strip() == "2"
    assert sidecar.read_text(encoding="utf-8") == old_entry
    assert list((memory / "tmp").iterdir()) == [sidecar]


def test_scan_cache_missing_directory_still_counts(tmp_path):
    source = (ROOT / "scripts" / "post-tool-hook.sh").read_text(encoding="utf-8")
    start = source.index("_pt_scan_lines() {")
    end = source.index("\nfi\n# CURRENT_LINES is 0 here", start)
    script = tmp_path / "scan.sh"
    script.write_text(
        'set -eu\nTRANSCRIPT="$1"\nREMEMBER_DIR="$2"\n'
        + source[start:end]
        + '\nprintf "%s\\n" "$CURRENT_LINES"\n',
        encoding="utf-8",
    )
    memory = tmp_path / "memory"
    memory.mkdir()
    transcript = tmp_path / "same.jsonl"
    transcript.write_bytes(b"one\ntwo\n")
    bash = str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe") if os.name == "nt" else "bash"
    proc = subprocess.run(
        [bash, str(script), str(transcript), str(memory)],
        capture_output=True,
        text=True,
        check=True,
        env=os.environ.copy(),
    )
    assert proc.stdout.strip() == "2"
    assert list(memory.iterdir()) == []


def test_scan_cache_temp_creation_failure_preserves_previous_entry(tmp_path):
    source = (ROOT / "scripts" / "post-tool-hook.sh").read_text(encoding="utf-8")
    start = source.index("_pt_scan_lines() {")
    end = source.index("\nfi\n# CURRENT_LINES is 0 here", start)
    script = tmp_path / "scan.sh"
    script.write_text(
        'set -eu\nTRANSCRIPT="$1"\nREMEMBER_DIR="$2"\n'
        + 'mktemp() { return 1; }\n'
        + source[start:end]
        + '\nprintf "%s\\n" "$CURRENT_LINES"\n',
        encoding="utf-8",
    )
    memory = tmp_path / "memory"
    (memory / "tmp").mkdir(parents=True)
    transcript = tmp_path / "same.jsonl"
    transcript.write_bytes(b"one\ntwo\n")
    sidecar = memory / "tmp" / "transcript-scan.same"
    old_entry = "0 0\nold-path\n"
    sidecar.write_text(old_entry, encoding="utf-8")
    bash = str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe") if os.name == "nt" else "bash"
    proc = subprocess.run(
        [bash, str(script), str(transcript), str(memory)],
        capture_output=True,
        text=True,
        check=True,
        env=os.environ.copy(),
    )
    assert proc.stdout.strip() == "2"
    assert sidecar.read_text(encoding="utf-8") == old_entry
    assert list((memory / "tmp").iterdir()) == [sidecar]


@pytest.mark.parametrize("directory", ["directory with spaces", "unicode-\u03bb"])
def test_scan_cache_preserves_transcript_path(tmp_path, directory):
    source = (ROOT / "scripts" / "post-tool-hook.sh").read_text(encoding="utf-8")
    start = source.index("_pt_scan_lines() {")
    end = source.index("\nfi\n# CURRENT_LINES is 0 here", start)
    script = tmp_path / "scan.sh"
    script.write_text(
        'set -eu\nTRANSCRIPT="$1"\nREMEMBER_DIR="$2"\n'
        + source[start:end]
        + '\nprintf "%s\\n" "$CURRENT_LINES"\n',
        encoding="utf-8",
    )
    memory = tmp_path / "memory"
    (memory / "tmp").mkdir(parents=True)
    transcript = tmp_path / directory / "same.jsonl"
    transcript.parent.mkdir()
    transcript.write_bytes(b"one\ntwo\n")
    sidecar = memory / "tmp" / "transcript-scan.same"
    bash = str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe") if os.name == "nt" else "bash"
    args = [bash, str(script), str(transcript), str(memory)]
    first = subprocess.run(args, capture_output=True, text=True, check=True, env=os.environ.copy())
    assert first.stdout.strip() == "2"
    assert sidecar.read_text(encoding="utf-8").splitlines() == ["8 2", str(transcript)]
    with transcript.open("ab") as f:
        f.write(b"three\n")
    second = subprocess.run(args, capture_output=True, text=True, check=True, env=os.environ.copy())
    assert second.stdout.strip() == "3"
    assert sidecar.read_text(encoding="utf-8").splitlines() == ["14 3", str(transcript)]
