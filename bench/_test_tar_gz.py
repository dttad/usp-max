"""Debug the tar.gz generation."""
import gzip
import io
import tarfile

# Build tar in memory
tar_buf = io.BytesIO()
tar = tarfile.open(fileobj=tar_buf, mode="w")
info = tarfile.TarInfo(name="urls-00001.txt")
data = b"http://a\nhttp://b\nhttp://c\n"
info.size = len(data)
tar.addfile(info, io.BytesIO(data))
tar.close()
print(f"tar size: {len(tar_buf.getvalue())}")
print(f"tar starts with: {tar_buf.getvalue()[:20]}")

# Now gzip the tar
gz_buf = io.BytesIO()
with gzip.GzipFile(fileobj=gz_buf, mode="wb", mtime=0) as gz:
    gz.write(tar_buf.getvalue())
print(f"gz size: {len(gz_buf.getvalue())}")
print(f"gz starts with: {gz_buf.getvalue()[:20]}")

# Re-open and verify
gz_buf.seek(0)
with gzip.open(gz_buf, "rb") as gz:
    tar_bytes = gz.read()
print(f"read tar bytes: {len(tar_bytes)}")
with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar2:
    print(f"tar2 contents: {tar2.getnames()}")
    for m in tar2.getmembers():
        f = tar2.extractfile(m)
        if f:
            print(f"  {m.name}: {f.read()[:50]}")