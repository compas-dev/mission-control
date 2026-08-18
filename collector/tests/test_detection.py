from __future__ import annotations

import sys
import unittest
from pathlib import Path

COLLECTOR_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(COLLECTOR_DIR))

import features  # noqa: E402
import parse  # noqa: E402
import registries  # noqa: E402
from collector.__main__ import build_history_snapshot, collect_material, merge_repos  # noqa: E402
from github import GitHub  # noqa: E402


class PythonSupportTests(unittest.TestCase):
    def test_requires_python_wins_over_minimal_ci_matrix(self):
        resolved = parse.resolve_pythons(
            ci=["3.10"],
            classifiers=["3.11", "3.12"],
            requires_python=">=3.11,<3.13",
        )

        self.assertEqual(
            resolved,
            {"versions": ["3.11", "3.12"], "source": "requires-python"},
        )

    def test_python_cells_report_declared_support(self):
        packaging = {
            "python_versions": ["3.11", "3.12"],
            "python_source": "requires-python",
        }

        py312 = features.detect(
            {"id": "py312", "kind": "python", "detect": {"version": "3.12"}},
            {},
            packaging,
            None,
            "arpastrana",
            "jax_fdm",
        )
        py313 = features.detect(
            {"id": "py313", "kind": "python", "detect": {"version": "3.13"}},
            {},
            packaging,
            None,
            "arpastrana",
            "jax_fdm",
        )

        self.assertEqual(py312["status"], "adopted")
        self.assertEqual(py313["status"], "not-adopted")
        self.assertEqual(py312["detail"], "requires-python: 3.11, 3.12")


class NodeMetadataTests(unittest.TestCase):
    def test_package_json_metadata_and_runtime_dependencies(self):
        package = parse.parse_package_json(
            """{
              "name": "@example/pkg",
              "version": "2.0.0",
              "engines": {"node": ">=20"},
              "packageManager": "pnpm@9.15.9",
              "dependencies": {"runtime-dep": "^1.0.0"},
              "peerDependencies": {"peer-dep": ">=2"},
              "devDependencies": {"dev-only": "^3"}
            }"""
        )

        self.assertEqual(package["node_engine"], ">=20")
        self.assertEqual(package["package_manager"], "pnpm@9.15.9")
        self.assertEqual(package["dependencies"], {"runtime-dep": "^1.0.0", "peer-dep": ">=2"})

    def test_prefixed_release_tag_is_comparable(self):
        self.assertEqual(parse.normalize_release_tag("compas-pb-ts-v2.0.0", "compas-pb-ts-v"), "2.0.0")
        self.assertEqual(parse.normalize_release_tag("v2.0.0"), "2.0.0")

    def test_registry_parsers(self):
        original = registries._json
        try:
            registries._json = lambda url: {
                "dist-tags": {"latest": "2.0.0"},
                "time": {"2.0.0": "2026-08-08T13:37:14.933Z"},
            }
            self.assertEqual(registries.latest("npm", "@example/pkg"), {"version": "2.0.0", "date": "2026-08-08"})

            registries._json = lambda url: {
                "latest": "2.0.0",
                "versions": {"2.0.0": {"createdAt": "2026-08-08T13:37:21Z"}},
            }
            self.assertEqual(registries.latest("jsr", "@example/pkg"), {"version": "2.0.0", "date": "2026-08-08"})
        finally:
            registries._json = original


class ApplicabilityTests(unittest.TestCase):
    def test_python_feature_is_na_for_node_project(self):
        class NoGitHubCalls:
            def search_code(self, *args, **kwargs):
                raise AssertionError("an inapplicable feature must not call GitHub")

        feature = {
            "id": "new-scene-api",
            "kind": "code",
            "applies_to": ["python"],
            "detect": {"present": ["from compas.scene"]},
        }
        cell = features.detect(feature, {"runtime": "node"}, {}, NoGitHubCalls(), "owner", "repo")

        self.assertEqual(cell["status"], "n/a")

    def test_registry_match_compares_all_distributions(self):
        release = {
            "github_tag": "2.0.0",
            "distributions": [
                {"registry": "npm", "version": "2.0.0"},
                {"registry": "jsr", "version": "2.0.0"},
            ],
        }
        cell = features.detect(
            {"id": "release-match", "kind": "registry-match"},
            {"runtime": "node"},
            {},
            None,
            "owner",
            "repo",
            release=release,
        )

        self.assertEqual(cell["status"], "adopted")
        self.assertIn("npm 2.0.0", cell["detail"])


class PartialCollectionTests(unittest.TestCase):
    def test_fresh_repo_replaces_only_matching_existing_entry(self):
        existing = [{"name": "compas", "stars": 1}, {"name": "compas_pb_ts", "stars": 2}]
        collected = [{"name": "compas_pb_ts", "stars": 3}]

        merged = merge_repos(existing, collected)

        self.assertEqual({repo["name"]: repo["stars"] for repo in merged}, {"compas": 1, "compas_pb_ts": 3})


class HistorySnapshotTests(unittest.TestCase):
    def test_snapshot_preserves_feature_definitions_and_per_repo_statuses(self):
        data = {
            "generated_at": "2026-08-14T12:34:56Z",
            "features": [
                {"id": "compas2", "label": "On COMPAS 2.x", "kind": "pin", "applies_to": ["python"]},
                {"id": "scene", "label": "New Scene API", "kind": "code", "applies_to": ["python"]},
            ],
            "repos": [
                {
                    "name": "example",
                    "health": {"staleness": "fresh", "ci": "passing", "open_issues": 2, "open_prs": 1},
                    "packaging": {"compas_major_floor": 2},
                    "features": {
                        "compas2": {"status": "adopted", "source": "auto", "detail": "compas >=2"},
                        "scene": {"status": "n/a", "source": "auto", "detail": "no matching API usage"},
                    },
                }
            ],
        }

        snapshot = build_history_snapshot(data)

        self.assertEqual(snapshot["schema_version"], 2)
        self.assertEqual(snapshot["date"], "2026-08-14")
        self.assertEqual(snapshot["features"], data["features"])
        self.assertEqual(snapshot["repos"]["example"]["features_adopted"], 1)
        self.assertEqual(snapshot["repos"]["example"]["features"], {"compas2": "adopted", "scene": "n/a"})


class MaterialCollectionTests(unittest.TestCase):
    def test_material_collection_uses_only_repository_metadata(self):
        class FakeGitHub:
            warnings = []

            def repo(self, owner, name):
                return {
                    "html_url": f"https://github.com/{owner}/{name}",
                    "archived": True,
                    "description": "Workshop material",
                    "language": "Python",
                    "stargazers_count": 4,
                    "pushed_at": "2025-03-12T11:58:14Z",
                    "homepage": "https://example.com",
                    "topics": ["compas", "workshop"],
                }

        material = collect_material(
            FakeGitHub(),
            {
                "name": "example-workshop",
                "owner": "example-org",
                "kind": "workshop",
                "category": "fabrication",
                "ecosystem_deps": ["compas_fab", "compas"],
            },
            {},
        )

        self.assertEqual(material["status"], "archived")
        self.assertEqual(material["kind"], "workshop")
        self.assertEqual(material["category"], "fabrication")
        self.assertEqual(material["last_activity_date"], "2025-03-12")
        self.assertEqual(material["ecosystem_deps"], ["compas", "compas_fab"])
        self.assertNotIn("health", material)
        self.assertNotIn("features", material)


class CodeSearchTests(unittest.TestCase):
    def test_github_search_adds_language_qualifier(self):
        gh = GitHub()
        request = {}

        def fake_get(path, params=None, retries=3):
            request.update(path=path, params=params)
            return {"total_count": 0}

        gh.get = fake_get

        self.assertFalse(
            gh.search_code(
                "arpastrana",
                "jax_fdm",
                "from compas.artists",
                language="Python",
            )
        )
        self.assertEqual(request["path"], "/search/code")
        self.assertEqual(
            request["params"]["q"],
            '"from compas.artists" repo:arpastrana/jax_fdm language:Python',
        )

    def test_absent_import_check_ignores_prose_only_match(self):
        class FakeGitHub:
            def __init__(self):
                self.queries = []

            def search_code(self, owner, name, pattern, language=None):
                self.queries.append((pattern, language))
                # Simulate a repo-wide prose hit for the old broad pattern.
                return pattern == "compas.artists"

        gh = FakeGitHub()
        feature = {
            "id": "no-deprecated-artist",
            "kind": "code",
            "detect": {
                "language": "Python",
                "absent": [
                    "from compas.artists",
                    "import compas.artists",
                    "from compas import artists",
                ],
            },
        }

        cell = features.detect(feature, {}, {}, gh, "arpastrana", "jax_fdm")

        self.assertEqual(cell["status"], "adopted")
        self.assertEqual(
            gh.queries,
            [
                ("from compas.artists", "Python"),
                ("import compas.artists", "Python"),
                ("from compas import artists", "Python"),
            ],
        )

    def test_scene_api_classifies_new_old_and_non_ui_repos(self):
        class FakeGitHub:
            def __init__(self, matches):
                self.matches = matches

            def search_code(self, owner, name, pattern, language=None):
                return pattern in self.matches

        feature = {
            "id": "new-scene-api",
            "kind": "code",
            "detect": {
                "language": "Python",
                "present": ["from compas.scene", "import compas.scene", "compas.scene."],
                "absent": ["from compas.artists", "import compas.artists", "compas.artists."],
                "no_match": "n/a",
            },
        }

        cases = [
            ({"from compas.scene"}, "adopted", "uses the Scene API"),
            ({"from compas.artists"}, "not-adopted", "still uses Artist"),
            ({"from compas.scene", "from compas.artists"}, "not-adopted", "migration is incomplete"),
            (set(), "n/a", "does not use either UI API"),
        ]

        for matches, expected, reason in cases:
            with self.subTest(reason=reason):
                cell = features.detect(feature, {}, {}, FakeGitHub(matches), "owner", "repo")
                self.assertEqual(cell["status"], expected)

    def test_fully_qualified_scene_reference_counts_as_usage(self):
        class FakeGitHub:
            def search_code(self, owner, name, pattern, language=None):
                return pattern == "compas.scene."

        feature = {
            "id": "new-scene-api",
            "kind": "code",
            "detect": {
                "language": "Python",
                "present": ["from compas.scene", "compas.scene."],
                "absent": ["from compas.artists", "compas.artists."],
                "no_match": "n/a",
            },
        }

        cell = features.detect(feature, {}, {}, FakeGitHub(), "owner", "repo")

        self.assertEqual(cell["status"], "adopted")

    def test_present_code_must_also_exclude_absent_patterns(self):
        class FakeGitHub:
            def search_code(self, owner, name, pattern, language=None):
                return pattern in {
                    "compas-dev/compas-actions/",
                    "compas-dev/compas-actions.",
                }

        feature = {
            "id": "compas-actions",
            "kind": "code",
            "detect": {
                "language": "YAML",
                "present": ["compas-dev/compas-actions/"],
                "absent": ["compas-dev/compas-actions."],
            },
        }

        cell = features.detect(feature, {}, {}, FakeGitHub(), "owner", "repo")

        self.assertEqual(cell["status"], "not-adopted")
        self.assertEqual(cell["detail"], "still uses 'compas-dev/compas-actions.'")

    def test_present_code_is_adopted_when_absent_patterns_are_clean(self):
        class FakeGitHub:
            def search_code(self, owner, name, pattern, language=None):
                return pattern == "compas-dev/compas-actions/"

        feature = {
            "id": "compas-actions",
            "kind": "code",
            "detect": {
                "language": "YAML",
                "present": ["compas-dev/compas-actions/"],
                "absent": ["compas-dev/compas-actions."],
            },
        }

        cell = features.detect(feature, {}, {}, FakeGitHub(), "owner", "repo")

        self.assertEqual(cell["status"], "adopted")


class FileDetectionTests(unittest.TestCase):
    def test_api_failure_is_unknown_not_missing(self):
        class FakeGitHub:
            def file_exists(self, owner, name, path, branch):
                return None if path == "mkdocs.yml" else False

        feature = {
            "id": "mkdocs",
            "kind": "file",
            "detect": {
                "any_of": ["mkdocs.yml"],
                "none_of": ["docs/conf.py"],
            },
        }

        cell = features.detect(feature, {}, {}, FakeGitHub(), "owner", "repo")

        self.assertEqual(cell["status"], "unknown")
        self.assertEqual(cell["detail"], "file check failed: mkdocs.yml")


class ReadmeDetectionTests(unittest.TestCase):
    FEATURE = {
        "id": "mission-control-badge",
        "kind": "readme",
        "detect": {
            "present": [
                "[![Made with COMPAS](https://compas.dev/badge.svg)]"
                "(https://compas.dev/mission-control/#{name})",
                "[![Made with COMPAS](https://compas.dev/badge-flat.svg)]"
                "(https://compas.dev/mission-control/#{name})",
            ],
        },
    }

    def test_badge_must_link_to_the_repo_detail_page(self):
        class FakeGitHub:
            def __init__(self, readme):
                self.readme = readme

            def readme_text(self, owner, name, ref=None):
                return self.readme

        cases = [
            (
                "[![Made with COMPAS](https://compas.dev/badge.svg)]"
                "(https://compas.dev/mission-control/#compas_fab)",
                "adopted",
            ),
            (
                '<p align="center">\n'
                '  <a href="https://compas.dev/mission-control/#compas_fab">'
                '<img src="https://compas.dev/badge.svg" alt="Made with COMPAS"></a>\n'
                "</p>",
                "adopted",
            ),
            ("[![Made with COMPAS](https://compas.dev/badge.svg)](https://compas.dev)", "not-adopted"),
            (
                '<a href="https://compas.dev"><img src="https://compas.dev/badge.svg" '
                'alt="Made with COMPAS"></a>',
                "not-adopted",
            ),
            ("See https://compas.dev/mission-control/#compas_fab", "not-adopted"),
        ]

        for readme, expected in cases:
            with self.subTest(expected=expected):
                cell = features.detect(self.FEATURE, {}, {}, FakeGitHub(readme), "compas-dev", "compas_fab")
                self.assertEqual(cell["status"], expected)

    def test_missing_and_unavailable_readmes_are_distinct(self):
        class FakeGitHub:
            def __init__(self, result):
                self.result = result

            def readme_text(self, owner, name, ref=None):
                return self.result

        missing = features.detect(self.FEATURE, {}, {}, FakeGitHub(False), "owner", "repo")
        unavailable = features.detect(self.FEATURE, {}, {}, FakeGitHub(None), "owner", "repo")

        self.assertEqual(missing["status"], "not-adopted")
        self.assertEqual(unavailable["status"], "unknown")


class GitHubReadmeTests(unittest.TestCase):
    def test_readme_endpoint_decodes_content_and_passes_ref(self):
        gh = GitHub()
        request = {}

        def fake_get(path, params=None, retries=3):
            request.update(path=path, params=params)
            return {"content": "IyBIZWxsbyE="}  # # Hello!

        gh.get = fake_get

        self.assertEqual(gh.readme_text("owner", "repo", "develop"), "# Hello!")
        self.assertEqual(request, {"path": "/repos/owner/repo/readme", "params": {"ref": "develop"}})

    def test_readme_endpoint_distinguishes_404_from_api_failure(self):
        missing = GitHub()
        missing.get = lambda *args, **kwargs: None

        failed = GitHub()

        def failed_get(*args, **kwargs):
            failed.warnings.append("request failed")
            return None

        failed.get = failed_get

        self.assertIs(missing.readme_text("owner", "repo"), False)
        self.assertIsNone(failed.readme_text("owner", "repo"))


if __name__ == "__main__":
    unittest.main()


class LocalScannerTests(unittest.TestCase):
    """The local scanner must be a drop-in for the API client it replaces."""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "src" / "demo_rhino").mkdir(parents=True)
        (self.root / ".github" / "workflows").mkdir(parents=True)
        (self.root / "docs").mkdir()
        (self.root / "src" / "demo.py").write_text("from compas.scene import Scene\n")
        (self.root / "src" / "legacy.py").write_text("from compas.artists import Artist\n")
        (self.root / "README.md").write_text("# Demo\n")
        (self.root / "mkdocs.yml").write_text("site_name: demo\n")
        (self.root / ".github" / "workflows" / "build.yml").write_text(
            "uses: compas-dev/compas-actions/build@v4\n"
        )
        # Noise that must not be scanned: vendored trees and non-Python files.
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "vendored.py").write_text("from compas.artists import X\n")
        (self.root / "docs" / "changelog.md").write_text("removed compas.artists usage\n")

    def scanner(self):
        import localrepo

        return localrepo.LocalScanner(GitHub(None), self.root)

    def test_file_and_dir_reads_match_the_api_surface(self):
        scanner = self.scanner()
        self.assertEqual(scanner.file_text("o", "n", "mkdocs.yml"), "site_name: demo\n")
        self.assertIsNone(scanner.file_text("o", "n", "missing.toml"))
        self.assertTrue(scanner.file_exists("o", "n", "mkdocs.yml"))
        self.assertFalse(scanner.file_exists("o", "n", "docs/conf.py"))
        self.assertEqual(scanner.dir_entries("o", "n", ".github/workflows"), ["build.yml"])
        self.assertEqual(scanner.dir_entries("o", "n", "nope"), [])
        self.assertEqual(scanner.readme_text("o", "n"), "# Demo\n")

    def test_absence_is_a_fact_not_a_failed_request(self):
        # The API client returns None when a request fails, which detection maps
        # to "unknown". A local tree can always answer, so it must never do that.
        self.assertIs(self.scanner().file_exists("o", "n", "docs/source/conf.py"), False)

    def test_language_filter_and_vendored_skips(self):
        scanner = self.scanner()
        # Restricted to Python: the changelog mentioning the old API is ignored,
        # and so is the vendored copy under node_modules.
        self.assertEqual(scanner.grep_files("compas.artists", language="Python"), ["src/legacy.py"])
        self.assertEqual(scanner.count_code("from compas.scene", language="Python"), 1)
        self.assertEqual(scanner.grep_files("compas-dev/compas-actions/", language="YAML"),
                         [str(Path(".github/workflows/build.yml"))])

    def test_dotted_patterns_match_literally(self):
        # The whole reason for scanning locally: '.' is a real character here,
        # not a tokeniser artefact.
        (self.root / "src" / "dotted.py").write_text("obj = compas.scene.Scene()\n")
        self.assertEqual(self.scanner().count_code("compas.scene.", language="Python"), 1)
        self.assertEqual(self.scanner().count_code("compas.zzz.", language="Python"), 0)

    def test_unknown_attributes_delegate_to_the_github_client(self):
        scanner = self.scanner()
        self.assertIsInstance(scanner.warnings, list)
        self.assertTrue(callable(scanner.repo))

    def test_path_traversal_is_refused(self):
        self.assertFalse(self.scanner().file_exists("o", "n", "../../etc/passwd"))

    def test_detection_runs_unchanged_against_the_scanner(self):
        scanner = self.scanner()
        cell = features.detect(
            {"id": "mkdocs", "kind": "file", "applies_to": ["python"],
             "detect": {"any_of": ["mkdocs.yml"], "none_of": ["docs/conf.py"]}},
            {}, {}, scanner, "o", "n",
        )
        self.assertEqual(cell["status"], "adopted")

        cell = features.detect(
            {"id": "no-deprecated-artist", "kind": "code", "applies_to": ["python"],
             "detect": {"language": "Python", "absent": ["from compas.artists"]}},
            {}, {}, scanner, "o", "n",
        )
        self.assertEqual(cell["status"], "not-adopted")
        self.assertIn("1 file", cell["detail"])  # counts, not just a boolean


class BatchedIssueCountTests(unittest.TestCase):
    """One GraphQL query must return exactly what the REST search path did."""

    def client(self, payload, record=None):
        gh = GitHub("token")

        def fake_graphql(query, retries=3):
            if record is not None:
                record.append(query)
            return payload

        gh.graphql = fake_graphql
        return gh

    def test_counts_and_oldest_age_are_unpacked_per_alias(self):
        import datetime

        created = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=100))
        payload = {
            "a0": {
                "issues": {"totalCount": 99},
                "pullRequests": {"totalCount": 14},
                "oldest": {"nodes": [{"createdAt": created.strftime("%Y-%m-%dT%H:%M:%SZ")}]},
            },
            "a1": {
                "issues": {"totalCount": 0},
                "pullRequests": {"totalCount": 0},
                "oldest": {"nodes": []},
            },
        }
        gh = self.client(payload)
        result = gh.batch_issue_pr_counts([("compas-dev", "compas"), ("o", "quiet")])

        self.assertEqual(result[("compas-dev", "compas")]["open_issues"], 99)
        self.assertEqual(result[("compas-dev", "compas")]["open_prs"], 14)
        self.assertEqual(result[("compas-dev", "compas")]["oldest_open_issue_age_days"], 100)
        # A repo with no open issues reports no age rather than zero.
        self.assertIsNone(result[("o", "quiet")]["oldest_open_issue_age_days"])

    def test_repos_are_chunked(self):
        queries = []
        gh = self.client({}, record=queries)
        gh.batch_issue_pr_counts([("o", f"r{i}") for i in range(60)], chunk=25)
        self.assertEqual(len(queries), 3)  # 25 + 25 + 10

    def test_unreadable_repo_is_omitted_so_the_caller_can_fall_back(self):
        # GraphQL nulls a single alias rather than failing the batch; that repo
        # must be absent from the result, not present with zeroes.
        gh = self.client({"a0": None, "a1": {"issues": {"totalCount": 3},
                                             "pullRequests": {"totalCount": 1},
                                             "oldest": {"nodes": []}}})
        result = gh.batch_issue_pr_counts([("o", "gone"), ("o", "fine")])
        self.assertNotIn(("o", "gone"), result)
        self.assertEqual(result[("o", "fine")]["open_issues"], 3)

    def test_graphql_requires_a_token(self):
        self.assertIsNone(GitHub(None).graphql("query { viewer { login } }"))
        self.assertEqual(GitHub(None).batch_issue_pr_counts([("o", "n")]), {})
