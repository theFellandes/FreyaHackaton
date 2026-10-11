"""Download only the released final kit; preserve archive bytes and validate extraction."""
from __future__ import annotations
import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
import uuid
import zipfile

RELEASE = datetime(2026, 10, 11, 17, 0, tzinfo=timezone.utc)
PAGE = "https://www.freyavoice.ai/hackathon"
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024

class ReleaseLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_submit = False
        self.links = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "section" and attrs.get("id") == "submit":
            self.in_submit = True
        if self.in_submit and tag == "a" and ".zip" in attrs.get("href", "").lower():
            self.links.append(urljoin(PAGE, attrs["href"]))
    def handle_endtag(self, tag):
        if tag == "section":
            self.in_submit = False

def require_release(now=None):
    now = now or datetime.now(timezone.utc)
    if now < RELEASE:
        raise ValueError("Final kit opens at 2026-10-11 17:00 UTC (20:00 Istanbul). No download attempted.")

def discover_url(html):
    parser = ReleaseLinks()
    parser.feed(html)
    urls = list(dict.fromkeys(u for u in parser.links if "build-the-builder-v" not in u.lower()))
    if len(urls) != 1:
        raise ValueError("No unique final ZIP link is published in the Submit tab. Supply the final email link with --url.")
    return urls[0]

def get(url, limit):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Download URL must be HTTPS without embedded credentials.")
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 FreyaSubmissionPreparation"})
    with urlopen(req, timeout=90) as response:
        if urlsplit(response.url).scheme != "https":
            raise ValueError("Download redirected to non-HTTPS.")
        chunks, size = [], 0
        while block := response.read(1024 * 1024):
            size += len(block)
            if size > limit:
                raise ValueError("Download exceeds the configured size bound.")
            chunks.append(block)
        return b"".join(chunks), response.url

def validate_members(archive):
    members, seen = [], set()
    total = 0
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        path = PurePosixPath(name)
        if (path.is_absolute() or ".." in path.parts or not path.parts
            or any(":" in p or "\x00" in p or p.endswith((" ", ".")) for p in path.parts)
            or any(re.fullmatch(r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", p, re.I) for p in path.parts)):
            raise ValueError("Unsafe ZIP member path.")
        key = str(path).casefold().rstrip("/")
        if key in seen:
            raise ValueError("Duplicate or case-colliding ZIP paths.")
        seen.add(key)
        if stat.S_ISLNK(info.external_attr >> 16):
            raise ValueError("ZIP symlinks are not accepted.")
        total += info.file_size
        if total > MAX_EXPANDED or len(seen) > 10000:
            raise ValueError("Expanded kit exceeds the size or member bound.")
        members.append((info, path))
    return members

def unpack(zip_path, destination):
    destination.mkdir(exist_ok=False)
    with zipfile.ZipFile(zip_path) as archive:
        members = validate_members(archive)
        briefs = [p for info, p in members if not info.is_dir() and p.name.casefold() == "brief.md"]
        if len(briefs) != 1:
            raise ValueError("This is not a final kit with one BRIEF.md; practice v3 is not the final kit.")
        case_root = briefs[0].parent
        readmes = [p for info, p in members if not info.is_dir() and p.parent == case_root and p.name.casefold() == "readme.md"]
        if len(readmes) != 1:
            raise ValueError("The final kit must have README.md beside BRIEF.md.")
        for info, path in members:
            target = destination.joinpath(*path.parts)
            target.resolve().relative_to(destination.resolve())
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
        return destination.joinpath(*case_root.parts), destination.joinpath(*briefs[0].parts), destination.joinpath(*readmes[0].parts)

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Final ZIP link from the Submit tab or latest email, not the practice v3 link.")
    parser.add_argument("--incoming", type=Path, default=Path(__file__).resolve().parents[1] / "incoming")
    args = parser.parse_args(argv)
    try:
        require_release()
        if args.url:
            page, page_url, url = b"", None, args.url
        else:
            page, page_url = get(PAGE, 5 * 1024 * 1024)
            url = discover_url(page.decode("utf-8"))
        if "build-the-builder-v" in url.lower():
            raise ValueError("The versioned starter kit is practice material, not the final release.")
        data, final_url = get(url, MAX_ARCHIVE)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        receipt_root = args.incoming.resolve() / stamp
        receipt_root.mkdir(parents=True, exist_ok=False)
        zip_path = receipt_root / "final-kit.zip"
        zip_path.write_bytes(data)
        if page:
            (receipt_root / "challenge-page.html").write_bytes(page)
        case_root, brief, readme = unpack(zip_path, receipt_root / "extracted")
        hashes = [{"path": p.relative_to(receipt_root).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                  for p in sorted((receipt_root / "extracted").rglob("*")) if p.is_file()]
        metadata = {"downloaded_at": datetime.now(timezone.utc).isoformat(), "source_url": final_url,
                    "page_url": page_url, "zip_sha256": hashlib.sha256(data).hexdigest(),
                    "case_folder": str(case_root), "brief": str(brief), "readme": str(readme),
                    "files": hashes}
        (receipt_root / "kit-receipt.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Required reading order: BRIEF first, then README.
        for title, path in (("BRIEF.md", brief), ("README.md", readme)):
            print("\n=== " + title + " ===\n" + path.read_text(encoding="utf-8-sig"))
        print(json.dumps({"receipt": str(receipt_root / "kit-receipt.json"), "case_folder": str(case_root)}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print("DOWNLOAD NOT COMPLETE: " + str(error), file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())