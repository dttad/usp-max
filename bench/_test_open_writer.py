"""Debug _open_writer with the docker file."""
import gzip
import io
import tarfile
import time
from pathlib import Path

path = Path("/tmp/test-tar.gz")
if path.exists():
    path.unlink()

# Mimic exactly what _open_writer does for compress=gz, use_tar=True
raw = path.open("wb")
fh = gzip.GzipFile(fileobj=raw, mode="wb", mtime=0)
tar = tarfile.open(fileobj=fh, mode="w")

inner_name = "urls-00001.txt"
info = tarfile.TarInfo(name=inner_name)
info.mtime = int(time.time())
info.size = 0

buf = io.BytesIO()

def write_bytes(data: bytes) -> None:
    buf.write(data)
    print(f"write_bytes: {len(data)} bytes (buf total: {buf.tell()})")

def finalize() -> None:
    info.size = buf.tell()
    print(f"finalize: buf has {buf.tell()} bytes")
    buf.seek(0)
    tar.addfile(info, buf)
    tar.close()
    fh.close()
    raw.close()
    print(f"finalize done")

# Simulate the for loop
for i in range(3):
    write_bytes(f"http://example.com/{i}\n".encode())

finalize()

# Verify
print(f"file size: {path.stat().st_size}")
import subprocess
out = subprocess.run(
    ["tar", "-xzvf", str(path), "-O", inner_name],
    capture_output=True, text=True,
)
print(f"tar -xzvf stdout: {out.stdout!r}")
print(f"tar -xzvf stderr: {out.stderr!r}")