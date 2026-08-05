from __future__ import annotations

import sys
import unittest
from pathlib import Path

COLLECTOR_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(COLLECTOR_DIR))

import features  # noqa: E402
import parse  # noqa: E402
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

    def test_scene_module_import_counts_as_new_scene_api(self):
        class FakeGitHub:
            def search_code(self, owner, name, pattern, language=None):
                return pattern == "from compas.scene"

        feature = {
            "id": "new-scene-api",
            "kind": "code",
            "detect": {
                "language": "Python",
                "present": ["from compas.scene", "import compas.scene"],
            },
        }

        cell = features.detect(feature, {}, {}, FakeGitHub(), "arpastrana", "jax_fdm")

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


if __name__ == "__main__":
    unittest.main()
