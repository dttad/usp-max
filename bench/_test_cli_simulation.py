"""Debug: simulate exactly what the CLI does for compress=gz, use_tar=True."""
import gzip
import os
import subprocess
import tarfile
import time
from pathlib import Path

# Create a path similar to the CLI output
out_dir = Path("/tmp/test-tar-debug")
out_dir.mkdir(parents=True, exist_ok=True)

path = out_dir / "urls-00001.txt.tar.gz"
if path.exists():
    path.unlink()

# Mimic the exact code from _open_writer
def _open_writer(path, compress, use_tar):
    if compress == "gz":
        raw = path.open("wb")
        import gzip as gz_mod
        fh = gz_mod.GzipFile(fileobj=raw, mode="wb", mtime=0)
        tar = None
        if use_tar:
            tar = tarfile.open(fileobj=fh, mode="w")
    else:
        raw = path.open("wb")
        fh = raw
        tar = None
        if use_tar:
            tar = tarfile.open(fileobj=fh, mode="w")

    if tar is not None:
        # Strip all outer suffixes to get inner filename
        name = path.name
        inner_name = name
        for suf in (".tar", ".gz", ".zst"):
            if inner_name.endswith(suf):
                inner_name = inner_name[: -len(suf)]
        print(f"DEBUG: inner filename = {inner_name}")
        info = tarfile.TarInfo(name=inner_name)
        info.mtime = int(time.time())
        info.size = 0

        import io
        buf = io.BytesIO()

        def write_bytes(data):
            buf.write(data)
            print(f"DEBUG write_bytes: buf total = {buf.tell()}")

        def finalize():
            print(f"DEBUG finalize: buf has {buf.tell()} bytes")
            info.size = buf.tell()
            buf.seek(0)
            tar.addfile(info, buf)
            print(f"DEBUG after tar.addfile")
            tar.close()
            print(f"DEBUG after tar.close")
            fh.close()
            print(f"DEBUG after fh.close")
            if fh is not raw:
                raw.close()
                print(f"DEBUG after raw.close")

        return raw, write_bytes, finalize
    # else...
    def write_bytes(data):
        fh.write(data)
    def finalize():
        fh.close()
        if fh is not raw:
            raw.close()
    return raw, write_bytes, finalize


# Simulate the CLI run
import json
_raw, current_writer, current_finalize = _open_writer(path, "gz", True)
current_count = 0

for i in range(5000):
    line = f"http://example.com/page-{i}\n"
    current_writer(line.encode("utf-8"))
    current_count += 1

current_finalize()

# Verify
print(f"\n=== file size: {path.stat().st_size} ===")
# Try to extract
import subprocess
out = subprocess.run(
    ["tar", "-xzvf", str(path), "-O", "urls-00001.txt"],
    capture_output=True, text=True,
)
print(f"tar -xzvf stdout (first 200): {out.stdout[:200]!r}")
print(f"tar -xzvf stderr (first 200): {out.stderr[:200]!r}")