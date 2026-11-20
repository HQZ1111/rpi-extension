"""Prepare/finalize GitHub-only releases; never publish crates or move version tags."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def api(path, missing_ok=False):
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if result.returncode:
        if missing_ok and "(HTTP 404)" in result.stderr:
            return None
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)


def select_tags(metadata, catalog, selection, repository):
    workspace = set(metadata["workspace_members"])
    packages = {p["name"]: p for p in metadata["packages"] if p["id"] in workspace}
    eligible = {entry["name"] for entry in catalog if entry.get("prebuilt_manifest")}
    names = sorted(eligible) if selection.strip() == "all" else sorted(set(
        filter(None, re.split(r"[\s,]+", selection.strip()))))
    if not names or set(names) - eligible:
        raise ValueError("Select 'all' or catalog packages with prebuilt manifests")
    tags = []
    for name in names:
        package = packages[name]
        if not any("cdylib" in target["crate_types"] for target in package["targets"]):
            raise ValueError(f"Not an extension library: {name}")
        if package["repository"].rstrip("/").removesuffix(".git") != f"https://github.com/{repository}":
            raise ValueError(f"Repository metadata mismatch: {name}")
        version = package["version"]
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ValueError(f"Expected a stable package version: {name} {version}")
        tags.append(f"{name}-v{version}")
    return tags


def check_tags(repository, tags, source):
    """Preflight every tag before creating anything; annotated tags are peeled."""
    existing = set()
    for tag in tags:
        ref = api(f"repos/{repository}/git/ref/tags/{quote(tag, safe='')}", missing_ok=True)
        if ref is None:
            continue
        target = ref["object"]
        while target["type"] == "tag":
            target = api(f"repos/{repository}/git/tags/{target['sha']}")["object"]
        if target["type"] != "commit" or target["sha"] != source:
            raise ValueError(f"Immutable tag {tag} points elsewhere; bump the package version or retry its exact source_ref")
        existing.add(tag)
    return existing


def release(repository, tag):
    # The REST /releases/tags endpoint does not find drafts. gh release view
    # resolves both drafts and published releases for the authenticated caller.
    result = subprocess.run(["gh", "release", "view", tag, "--repo", repository,
                             "--json", "isDraft,assets"], capture_output=True, text=True)
    if result.returncode:
        if result.stderr.strip() == "release not found":
            return None
        raise RuntimeError(result.stderr.strip())
    item = json.loads(result.stdout)
    item["draft"] = item.pop("isDraft")
    return item


def prepare(repository, tags, source):
    existing = check_tags(repository, tags, source)
    for tag in tags:
        if tag not in existing:
            subprocess.run(["gh", "api", "--method", "POST", f"repos/{repository}/git/refs",
                            "-f", f"ref=refs/tags/{tag}", "-f", f"sha={source}"],
                           check=True, stdout=subprocess.DEVNULL)
        if release(repository, tag) is None:
            subprocess.run(["gh", "release", "create", tag, "--repo", repository,
                            "--verify-tag", "--draft", "--title", tag,
                            "--notes", f"GitHub-only native extension build from {source}. "
                            "No crates.io package was published. Assets are validated across all supported targets."], check=True)


def finalize(repository, tags, source):
    if check_tags(repository, tags, source) != set(tags):
        raise ValueError("Release source tags are missing; leaving drafts unpublished")
    releases = [(tag, release(repository, tag)) for tag in tags]
    for tag, item in releases:
        if item is None or not any(asset["name"] == "rpi-extension.json" for asset in item["assets"]):
            raise ValueError(f"Validated discovery manifest missing: {tag}; leaving drafts unpublished")
    for tag, item in releases:
        if item["draft"]:
            subprocess.run(["gh", "release", "edit", tag, "--repo", repository,
                            "--draft=false", "--latest=false"], check=True)
        print(f"https://github.com/{repository}/releases/tag/{tag}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "prepare", "finalize"])
    parser.add_argument("--packages", default="all")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--source-ref")
    parser.add_argument("--tags", help="JSON array from the prepare job (finalize only)")
    args = parser.parse_args()
    if not args.repository or not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repository):
        parser.error("--repository owner/repo is required")
    source = args.source_ref or command("git", "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", source):
        parser.error("source-ref must be an exact commit SHA")
    if args.command == "finalize":
        tags = json.loads(args.tags or "[]")
        if not isinstance(tags, list) or not tags or not all(
                isinstance(tag, str) and re.fullmatch(r"[\w-]+-v\d+\.\d+\.\d+", tag) for tag in tags):
            parser.error("finalize requires a non-empty array of package version tags")
        finalize(args.repository, tags, source)
        return
    if source != command("git", "rev-parse", "HEAD"):
        parser.error("prepare must run in the exact source checkout")
    metadata = json.loads(command("cargo", "metadata", "--no-deps", "--locked", "--format-version", "1"))
    tags = select_tags(metadata, json.loads(Path("catalog/packages.json").read_text()), args.packages, args.repository)
    print(json.dumps({"source_ref": source, "package_releases": tags}))
    if args.command == "prepare":
        prepare(args.repository, tags, source)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                output.write(f"source_ref={source}\npackage_releases={json.dumps(tags)}\n")


if __name__ == "__main__":
    main()
