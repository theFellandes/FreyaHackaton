"""Preserve and safely combine original Drive downloads without changing their bytes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from download_release import validate_members


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", action="append", type=Path, default=[])
    parser.add_argument("--file", action="append", nargs=2, metavar=("SOURCE", "CASE_PATH"), default=[])
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    case = project / "submission/case"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    originals = project / "submission/incoming" / stamp
    originals.mkdir(parents=True, exist_ok=False)
    case.mkdir(parents=True, exist_ok=True)
    added: list[dict[str, str]] = []

    def install(source: Path, relative: Path) -> None:
        target = case / relative
        target.resolve().relative_to(case.resolve())
        target.parent.mkdir(parents=True, exist_ok=True)
        before = digest(target) if target.exists() else None
        after = digest(source)
        if before and before != after:
            old = originals / "previous-case" / relative
            old.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, old)
        shutil.copyfile(source, target)
        assert digest(target) == after
        added.append({"path": target.relative_to(project).as_posix(), "sha256": after,
                      "source": source.relative_to(project).as_posix()})

    archives = []
    for index, supplied in enumerate(args.archive):
        retained = originals / supplied.name
        shutil.copyfile(supplied, retained)
        archives.append({"path": retained.relative_to(project).as_posix(), "sha256": digest(retained)})
        expanded = originals / f"archive-{index}"
        expanded.mkdir()
        with zipfile.ZipFile(retained) as archive:
            for info, relative in validate_members(archive):
                target = expanded.joinpath(*relative.parts)
                target.resolve().relative_to(expanded.resolve())
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                install(target, Path(*relative.parts))
    for index, (supplied, relative_text) in enumerate(args.file):
        supplied_path = Path(supplied)
        relative = Path(relative_text)
        retained = originals / "files" / str(index) / supplied_path.name
        retained.parent.mkdir(parents=True)
        shutil.copyfile(supplied_path, retained)
        install(retained, relative)
    receipt = {"assembled_at_utc": datetime.now(timezone.utc).isoformat(),
               "source_url": "https://drive.google.com/drive/folders/1r9KOek3SXiE0mu1wBofezD0bjiAJhcYQ",
               "case_folder": str(case), "archives": archives, "installed_files": added,
               "note": "Original files combined without byte edits; inventory completeness is checked separately."}
    receipt_path = originals / "assembly-receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"case_folder": str(case), "receipt": str(receipt_path), "file_count": len(added)}))


if __name__ == "__main__":
    main()
