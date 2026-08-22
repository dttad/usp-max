"""Debug tar+gzip streaming pattern."""
import gzip
import io
import tarfile
from pathlib import Path

path = Path("/tmp/test-out/urls-00001.txt.tar.gz")
path.parent.mkdir(parents=True, exist_ok=True)

raw = path.open("wb")
fh = gzip.GzipFile(fileobj=raw, mode="wb", mtime=0)
tar = tarfile.open(fileobj=fh, mode="w")

info = tarfile.TarInfo(name="urls-00001.txt")
info.size = 0  # unknown yet
buf = io.BytesIO()


def write_bytes(data: bytes) -> None:
    buf.write(data)


def finalize() -> None:
    info.size = buf.tell()
    buf.seek(0)
    tar.addfile(info, buf)
    tar.close()
    fh.close()
    raw.close()


write_bytes(b"http://a\nhttp://b\n")
write_bytes(b"http://c\n")
finalize()

# Verify
print(f"file size: {path.stat().st_size}")
import subprocess

# Extract
out = subprocess.run(
    ["tar", "-xzvf", str(path), "-O", "urls-00001.txt"],
    capture_output=True, text=True,
)
print(f"stdout: {out.stdout!r}")
print(f"stderr: {out.stderr!r}")