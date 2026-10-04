"""Verify the frozen public assets; optionally publish and download-check them."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile

VERSION = "0.2.0-preview.2"
TAG = "v" + VERSION
REPOSITORY = "yucheng-chang-yc/Project-Relay"
ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "release" / TAG
EXPECTED = {
    "Project-Relay-Local-v0.2.0-preview.2.zip": "48f458447cf9cc8da8c9015799a3001708db3343fa9196e8b30fc8f9edcac6c0",
    "Project-Relay-Plugin-Template-v0.2.0-preview.2.zip": "ddd3f67e1b4d7402a191c4dff3a81b44f34c9bc66951573ce3d618d8a622d1bc",
    "README.md": "965155d5ae01a7920f2d928735d6f3b0b470741c83e5b871fb8ec541240f5ecc",
    "INSTALL.md": "8b9016b16f3029b72277fba7ee3c5c5fc5829483b6a0c0aa57d577fc1f267e70",
    "RELEASE_NOTES.md": "c7ca872a41e5c1f983275c94f3f20e9bc1f187aea41c57d050a4e35d93a02bb1",
    "LICENSE": "cf0470827d18c98442f9ae28173e5dc582928f0942b64408d22526086442d489",
    "SHA256SUMS.txt": "7458d8730ffb18cbab728d69b8fe2dd08176ad16a6d1cf516eb8fdc064afbe5b",
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def verify_assets(directory):
    require({p.name for p in directory.iterdir()} == set(EXPECTED), "Expected exactly seven public release files")
    for name, digest in EXPECTED.items():
        require(hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest, "SHA-256 mismatch: " + name)
    sums = {line.split("  ", 1)[1]: line.split("  ", 1)[0]
            for line in (directory / "SHA256SUMS.txt").read_text().splitlines()}
    require(sums == {k: v for k, v in EXPECTED.items() if k != "SHA256SUMS.txt"}, "Checksum inventory differs")


def verify_source():
    pack = ROOT / "local-pack"
    prefix = "Project-Relay-Local-v" + VERSION + "/"
    with zipfile.ZipFile(ASSETS / (prefix[:-1] + ".zip")) as archive:
        names = archive.namelist()
        require(len(names) == 86 and len(set(names)) == 86, "Unexpected Local ZIP membership")
        paths = set()
        for name in names:
            require(name.startswith(prefix), "Unexpected archive prefix")
            relative = name[len(prefix):]
            require(".." not in Path(relative).parts, "Unsafe archive member")
            paths.add(relative)
            require((pack / relative).read_bytes() == archive.read(name), "Source differs from accepted ZIP: " + relative)
        actual = {p.relative_to(pack).as_posix() for p in pack.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
        require(actual == paths, "Frozen source membership differs")
        manifest = json.loads(archive.read(prefix + "PACKAGE_MANIFEST.json"))
        require(manifest["package_version"] == VERSION, "Package version mismatch")
        require(len(manifest["files"]) == 85, "Manifest count mismatch")
        for entry in manifest["files"]:
            raw = archive.read(prefix + entry["path"])
            require(len(raw) == entry["bytes"] and hashlib.sha256(raw).hexdigest() == entry["sha256"], "Manifest member mismatch")
    with zipfile.ZipFile(ASSETS / ("Project-Relay-Plugin-Template-v" + VERSION + ".zip")) as archive:
        require(len(archive.namelist()) == 4, "Template membership mismatch")
        require(not any(name.endswith(".app.json") for name in archive.namelist()), "Template must be unbound")
        pfx = "Project-Relay-Plugin-Template-v" + VERSION + "/"
        require(json.loads(archive.read(pfx + "plugin.json"))["version"] == VERSION, "Template version mismatch")
        require(archive.read(pfx + "LICENSE") == (ROOT / "LICENSE").read_bytes(), "Template license mismatch")
    for name in ("README.md", "INSTALL.md", "RELEASE_NOTES.md", "LICENSE"):
        require((ROOT / name).read_bytes() == (ASSETS / name).read_bytes(), "Accompanying document mismatch")


def gh(*args):
    result = subprocess.run(["gh", *args], check=True, text=True, capture_output=True)
    return result.stdout


def api(endpoint):
    return json.loads(gh("api", "repos/" + REPOSITORY + "/" + endpoint))


def publish():
    require(os.environ.get("GITHUB_REPOSITORY") == REPOSITORY, "Unexpected repository")
    require(os.environ.get("GITHUB_REF") == "refs/heads/main", "Publish only from main")
    commit = os.environ["GITHUB_SHA"]
    existing = [r for r in api("releases?per_page=100") if r["tag_name"] == TAG]
    require(len(existing) <= 1, "Ambiguous existing release")
    if not existing:
        gh("release", "create", TAG, "--repo", REPOSITORY, "--target", commit,
           "--draft", "--prerelease", "--latest=false", "--title", "Project Relay " + TAG,
           "--notes-file", str(ASSETS / "RELEASE_NOTES.md"))
    release = api("releases/tags/" + TAG)
    ref = api("git/ref/tags/" + TAG)
    require(ref["object"]["type"] == "commit" and ref["object"]["sha"] == commit, "Release tag points to a different commit")
    require(release["name"] == "Project Relay " + TAG and release["prerelease"], "Existing release identity mismatch")
    remote = {asset["name"]: asset for asset in release["assets"]}
    require(set(remote) <= set(EXPECTED), "Unexpected existing release assets")
    missing = [name for name in EXPECTED if name not in remote]
    if missing:
        require(release["draft"], "Do not alter a previously published incomplete release")
        gh("release", "upload", TAG, *[str(ASSETS / name) for name in missing], "--repo", REPOSITORY)
    with tempfile.TemporaryDirectory(prefix="relay-release-verify-") as directory:
        gh("release", "download", TAG, "--repo", REPOSITORY, "--dir", directory)
        verify_assets(Path(directory))
    if release["draft"]:
        gh("release", "edit", TAG, "--repo", REPOSITORY, "--draft=false", "--prerelease", "--latest=false")
    release = api("releases/tags/" + TAG)
    require(not release["draft"] and release["prerelease"], "Release publication state mismatch")
    require({a["name"] for a in release["assets"]} == set(EXPECTED), "Published asset set mismatch")
    report = {"release_url": release["html_url"], "commit": commit, "prerelease": True,
              "download_verified_assets": EXPECTED}
    print(json.dumps(report, indent=2))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("Published " + release["html_url"] + "\n\nAll seven assets were downloaded and SHA-256 verified.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    verify_assets(ASSETS)
    verify_source()
    if args.publish:
        publish()
    else:
        print(json.dumps({"status": "PASS", "source_files": 86, "public_assets": 7, "sha256": EXPECTED}, indent=2))
