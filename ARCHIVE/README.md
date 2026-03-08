# Release archives

Tarballs are **not** committed to the repo (they are gitignored to stay under GitHub’s 100 MB limit).

**Build a tarball locally when needed:**

```bash
tar --exclude='.git' --exclude='v139_work' --exclude='ARCHIVE' -czvf taknet-aggregator-vX.Y.Z.tar.gz .
```

Upload the resulting file yourself (e.g. as a GitHub Release asset).
