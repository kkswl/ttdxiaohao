"""Build from an explicit allowlist. Test fixtures, runtime state and secrets are excluded."""

import hashlib
import json
import zipfile
from pathlib import Path

from validate import validate_manifest


def package():
    root = Path(__file__).resolve().parents[1]
    manifest, _, _ = validate_manifest(root)
    files = [root / "plugin.json", root / "plugin.py", root / "README.md"]
    files += sorted((root / "account_manager").glob("*.py"))
    files += sorted((root / "docs").glob("*.md"))
    assert all(f.is_file() and not f.is_symlink() for f in files)
    names = [f.relative_to(root).as_posix() for f in files]
    assert len(names) == len(set(n.casefold() for n in names)) <= 500
    assert sum(f.stat().st_size for f in files) <= 50 * 1024 * 1024
    output = root.parent / "dist"
    output.mkdir(exist_ok=True)
    target = output / (manifest["id"] + "-" + manifest["version"] + "-preview.zip")
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file, name in zip(files, names):
            info = zipfile.ZipInfo(name, date_time=(2026, 11, 20, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, file.read_bytes())
    assert target.stat().st_size <= 10 * 1024 * 1024
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == names
        for name in names:
            assert not any(piece in name for piece in ("__pycache__", ".sqlite", "settings.json", "tests/", ".env"))
    source = output / (manifest["id"] + "-" + manifest["version"] + "-source.zip")
    source_files = files + sorted((root / "tests").glob("*.py")) + sorted((root / "tools").glob("*.py"))
    source_files += [root / "requirements-dev.txt", root / ".gitignore", root / ".dockerignore",
                     root / "tools" / "Dockerfile.test"]
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in source_files:
            info = zipfile.ZipInfo(file.relative_to(root).as_posix(), date_time=(2026, 11, 20, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, file.read_bytes())
    with zipfile.ZipFile(source) as archive:
        assert archive.testzip() is None
        assert len(archive.namelist()) == len(set(n.casefold() for n in archive.namelist()))
    print(json.dumps([{"path": str(path), "files": count, "bytes": path.stat().st_size,
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                      for path, count in ((target, len(names)), (source, len(source_files)))], ensure_ascii=False))


if __name__ == "__main__":
    package()
