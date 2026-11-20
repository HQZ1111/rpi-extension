"""GitHub-only releases must be source-bound, opt-in and safe to retry."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("github_release", Path(__file__).with_name("github-release.py"))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class GitHubReleaseTests(unittest.TestCase):
    repository = "pi-rust/rpi-extension"
    source = "a" * 40
    tag = "demo-v1.2.3"

    def test_select_catalog_extensions_only_and_validate_repository(self):
        package = dict(id="demo", name="demo", version="1.2.3", repository=f"https://github.com/{self.repository}",
                       targets=[{"crate_types": ["cdylib"]}])
        metadata = dict(packages=[package], workspace_members=["demo"])
        catalog = [dict(name="demo", prebuilt_manifest="manifest")]
        self.assertEqual(release.select_tags(metadata, catalog, "all", self.repository), [self.tag])
        self.assertEqual(release.select_tags(metadata, catalog, "demo, demo", self.repository), [self.tag])
        for selection in ["", "not-in-catalog"]:
            with self.assertRaises(ValueError):
                release.select_tags(metadata, catalog, selection, self.repository)
        package["repository"] = "https://github.com/pi-rust/rpi-package"
        with self.assertRaises(ValueError):
            release.select_tags(metadata, catalog, "all", self.repository)

    def test_reject_tag_collision_before_any_mutation(self):
        with mock.patch.object(release, "api", return_value={"object": {"type": "commit", "sha": "b" * 40}}), \
                mock.patch.object(release.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "Immutable tag"):
                release.prepare(self.repository, [self.tag], self.source)
            run.assert_not_called()

    def test_new_release_is_draft_and_tag_is_exact_source(self):
        with mock.patch.object(release, "api", return_value=None), mock.patch.object(release.subprocess, "run") as run:
            release.prepare(self.repository, [self.tag], self.source)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn(f"sha={self.source}", commands[0])
            self.assertIn("--draft", commands[1])
            self.assertIn("--verify-tag", commands[1])

    def test_retry_existing_release_never_moves_tag(self):
        with mock.patch.object(release, "check_tags", return_value={self.tag}), \
                mock.patch.object(release, "release", return_value={"draft": True}), \
                mock.patch.object(release.subprocess, "run") as run:
            release.prepare(self.repository, [self.tag], self.source)
            run.assert_not_called()

    def test_annotated_tag_is_peeled(self):
        with mock.patch.object(release, "api", side_effect=[
                {"object": {"type": "tag", "sha": "c" * 40}},
                {"object": {"type": "commit", "sha": self.source}}]):
            self.assertEqual(release.check_tags(self.repository, [self.tag], self.source), {self.tag})

    def test_finalize_requires_all_manifests_before_any_publication(self):
        with mock.patch.object(release, "check_tags", return_value={self.tag, "other-v1.0.0"}), \
                mock.patch.object(release, "release", side_effect=[
                    {"draft": True, "assets": [{"name": "rpi-extension.json"}]},
                    {"draft": True, "assets": []}]), \
                mock.patch.object(release.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "manifest missing"):
                release.finalize(self.repository, [self.tag, "other-v1.0.0"], self.source)
            run.assert_not_called()

    def test_finalize_publishes_validated_draft_without_setting_latest(self):
        with mock.patch.object(release, "check_tags", return_value={self.tag}), \
                mock.patch.object(release, "release", return_value={"draft": True, "assets": [{"name": "rpi-extension.json"}]}), \
                mock.patch.object(release.subprocess, "run") as run:
            release.finalize(self.repository, [self.tag], self.source)
            self.assertIn("--draft=false", run.call_args.args[0])
            self.assertIn("--latest=false", run.call_args.args[0])

    def test_finalize_rejects_missing_tag(self):
        with mock.patch.object(release, "check_tags", return_value=set()), \
                mock.patch.object(release.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "tags are missing"):
                release.finalize(self.repository, [self.tag], self.source)
            run.assert_not_called()

    def test_network_errors_are_not_treated_as_missing_tags(self):
        for stderr, expected in [("gh: Not Found (HTTP 404)", None), ("timeout", RuntimeError)]:
            with mock.patch.object(release.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", stderr)):
                if expected is None:
                    self.assertIsNone(release.api("example", missing_ok=True))
                else:
                    with self.assertRaises(expected):
                        release.api("example", missing_ok=True)


if __name__ == "__main__":
    unittest.main()
