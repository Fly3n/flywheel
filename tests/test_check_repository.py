"""Regression fixtures for structure checks; no capability scripts are run."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_repository.py"
SPEC = importlib.util.spec_from_file_location("check_repository", SCRIPT)
checker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = checker
SPEC.loader.exec_module(checker)


class RepositoryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for filename in checker.REQUIRED_DOCUMENTS + checker.REQUIRED_MAINTENANCE_FILES:
            self.write(filename, "# Repository document\n")
        self.index()

    def write(self, path: str, text: str) -> None:
        destination = self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")

    def skill(self, path: str, *, standalone: bool = True, body: str = "") -> None:
        name = path.split("/")[-1]
        self.write(f"{path}/SKILL.md", f"---\nname: {name}\ndescription: A useful capability.\n---\n\n{body}")
        if standalone:
            self.write(f"{path}/README.md", f"# {name}\n")

    def index(self, *capabilities: str) -> None:
        rows = "".join(f"| [{path}]({path}/README.md) | Solves a problem |\n" for path in capabilities)
        self.write("README.md", "# Repository\n\n| 能力 | 解决什么问题 |\n| --- | --- |\n" + rows)

    def check(self, **kwargs):
        return checker.check_repository(self.root, **kwargs)

    def codes(self, result) -> list[str]:
        return [issue.code for issue in result.issues]

    def assertValid(self, result) -> None:
        self.assertEqual(result.issues, [], "\n".join(map(str, result.issues)))

    def test_standalone_and_suite_accept_internal_resources(self) -> None:
        self.skill("skills/example", body="[Asset](assets/file%20(1).md#section)\n")
        self.write("skills/example/assets/file (1).md", "# Section\n")
        self.skill("suite/first", standalone=False, body="[Other unit](../second/SKILL.md)\n")
        self.skill("suite/second", standalone=False)
        self.write("suite/README.md", "[First](first/SKILL.md)\n")
        self.index("skills/example", "suite")
        result = self.check()
        self.assertValid(result)
        self.assertEqual(result.capabilities, {"skills/example", "suite"})

    def test_missing_required_files_and_incomplete_capabilities(self) -> None:
        self.skill("skills/example")
        (self.root / "skills/example/SKILL.md").unlink()
        (self.root / "docs/maintenance.md").unlink()
        self.skill("suite/first", standalone=False)
        self.index("skills/example", "suite")
        missing = {issue.path for issue in self.check().issues if issue.code == "required-file"}
        self.assertEqual(missing, {"skills/example/SKILL.md", "suite/README.md", "docs/maintenance.md"})

    def test_extra_markdown_is_rejected_only_at_repository_root(self) -> None:
        self.write("notes.md", "# Extra root document\n")
        self.write("REPORT.MD", "# Extra root document\n")
        self.write("docs/guide.md", "# Maintenance guide\n")
        self.skill("suite/first", standalone=False)
        self.write("suite/README.md", "# Suite\n")
        self.index("suite")
        result = self.check()
        self.assertEqual({issue.path for issue in result.issues if issue.code == "root-markdown"}, {"notes.md", "REPORT.MD"})
        inventory = checker.make_inventory(self.root).files - {"notes.md", "REPORT.MD"}
        self.assertValid(self.check(tracked=True, file_inventory=inventory))

    def test_empty_standalone_directory_is_not_silently_accepted(self) -> None:
        (self.root / "skills/empty").mkdir(parents=True)
        result = self.check()
        self.assertIn("skills/empty", result.capabilities)
        self.assertEqual(sum(code == "required-file" for code in self.codes(result)), 2)
        self.assertIn("index-missing", self.codes(result))

    def test_frontmatter_requires_name_description_and_matching_directory(self) -> None:
        self.skill("skills/example")
        self.index("skills/example")
        cases = (
            ("name: example\ndescription: text\n", "frontmatter"),
            ("---\nname: example\ndescription: text\n", "frontmatter"),
            ("---\nname: another\ndescription: text\n---\n", "frontmatter-name"),
            ("---\nname: example\n---\n", "frontmatter"),
            ("---\nname: example\ndescription: []\n---\n", "frontmatter"),
            ("---\nname: example\ndescription: ''\n---\n", "frontmatter"),
            ("---\nname: example\nname: example\ndescription: text\n---\n", "frontmatter"),
        )
        for text, expected_code in cases:
            with self.subTest(text=text):
                self.write("skills/example/SKILL.md", text)
                self.assertIn(expected_code, self.codes(self.check()))

    def test_frontmatter_accepts_quoted_and_multiline_strings(self) -> None:
        self.skill("skills/example")
        self.index("skills/example")
        for description in ('"A quoted value: with colon" # comment', "'It''s useful.'", ">-\n  A longer description\n  on another line.", "|\n  Multi-line description."):
            with self.subTest(description=description):
                self.write("skills/example/SKILL.md", f"---\nname: 'example'\ndescription: {description}\nextra: allowed\n---\n")
                self.assertValid(self.check())

    def test_directory_names_are_checked_for_units_and_suites(self) -> None:
        self.skill("skills/Bad_Name")
        self.skill("BadSuite/bad_unit", standalone=False)
        self.write("BadSuite/README.md", "# Suite\n")
        self.index("skills/Bad_Name", "BadSuite")
        paths = {issue.path for issue in self.check().issues if issue.code == "directory-name"}
        self.assertTrue({"skills/Bad_Name", "BadSuite", "BadSuite/bad_unit"} <= paths)

    def test_missing_inline_image_and_reference_destinations_are_reported(self) -> None:
        self.skill("skills/example", body="[Inline](missing.md)\n![Image](missing.png)\n[Reference][asset]\n[asset]: absent.md \"Title\"\n[Undefined][unknown]\n")
        self.index("skills/example")
        result = self.check()
        targets = [issue for issue in result.issues if issue.code == "link-target"]
        self.assertEqual(len(targets), 4)
        self.assertEqual({issue.line for issue in targets}, {6, 7, 8, 9})
        self.assertIn("link-reference", self.codes(result))

    def test_markdown_examples_html_templates_and_external_links_are_ignored(self) -> None:
        body = r"""[Web](https://example.invalid/page)
[Mail](mailto:test@example.invalid)
[App](app://example)
[Protocol](//example.invalid/path)
[Anchor](#section)
[Template](references/<name>.md)
[Template variable](references/${name}.md)
[Template braces](references/{{name}}.md)
[Template simple braces](references/{name}.md)
[Ellipsis](references/.../example.md)
<a href="missing.md">HTML link</a>
<!-- [Comment](missing.md) -->
<code>[HTML code](missing.md)</code>
`[Inline code](missing.md)`
```md
[Fence](missing.md)
```
~~~md
[Other fence](missing.md)
~~~
    [Indented code](missing.md)
\[Escaped](missing.md)
"""
        self.skill("skills/example", body=body)
        self.index("skills/example")
        self.assertValid(self.check())

    def test_valid_reference_and_angle_wrapped_paths_with_spaces(self) -> None:
        self.skill("skills/example", body="[Read][asset]\n[asset]\n[asset]: <assets/with spaces.md> \"Title\"\n[Inline](<assets/with spaces.md>)\n")
        self.write("skills/example/assets/with spaces.md", "# Asset\n")
        self.index("skills/example")
        self.assertValid(self.check())

    def test_capability_cannot_reference_another_capability_or_root(self) -> None:
        self.skill("skills/first", body="[Root](../../README.md)\n[Other](../second/SKILL.md)\n")
        self.skill("skills/second")
        self.index("skills/first", "skills/second")
        issues = [issue for issue in self.check().issues if issue.code == "link-boundary"]
        self.assertEqual(len(issues), 2)

    def test_absolute_and_repository_escape_links_are_reported(self) -> None:
        self.write("docs/maintenance.md", "[Absolute](/README.md)\n[Windows](C:/README.md)\n[Escape](../../missing.md)\n")
        self.assertEqual(self.codes(self.check()).count("link-path"), 3)

    def test_index_detects_missing_duplicate_unknown_and_wrong_entry_target(self) -> None:
        self.skill("skills/first")
        self.skill("skills/second")
        self.index("skills/first", "skills/first", "skills/unknown")
        result = self.check()
        self.assertTrue({"index-missing", "index-duplicate", "index-target"} <= set(self.codes(result)))
        self.write("README.md", "| 能力 | 解决什么问题 |\n| --- | --- |\n| [First](skills/first/SKILL.md) | Purpose |\n")
        self.assertIn("index-target", self.codes(self.check()))

    def test_index_requires_exactly_one_two_column_table(self) -> None:
        for text in (
            "# No table\n",
            "| 能力 | 解决什么问题 | Extra |\n| --- | --- | --- |\n",
            "| 能力 | 解决什么问题 |\n| --- | --- |\n\n| 能力 | 解决什么问题 |\n| --- | --- |\n",
            "| 能力 | 解决什么问题 |\n| --- |\n",
            "| 能力 | 解决什么问题 |\n| --- | --- |\n| Nothing | |\n",
        ):
            with self.subTest(text=text):
                self.write("README.md", text)
                self.assertIn("index-table", self.codes(self.check()))

    def test_index_can_link_to_directory_and_use_references(self) -> None:
        self.skill("skills/example")
        self.write("README.md", "| 能力 | 解决什么问题 |\n| --- | --- |\n| [Example][example] | Purpose |\n\n[example]: skills/example/\n")
        self.assertValid(self.check())

    def test_tracked_scope_rejects_readme_links_to_existing_untracked_capability(self) -> None:
        self.skill("skills/published")
        self.skill("skills/untracked")
        self.index("skills/published", "skills/untracked")
        inventory = [path for path in checker.make_inventory(self.root).files if not path.startswith("skills/untracked/")]
        self.assertValid(self.check())
        result = self.check(tracked=True, file_inventory=inventory)
        self.assertEqual(result.capabilities, {"skills/published"})
        self.assertTrue({"index-target", "link-target"} <= set(self.codes(result)))
        self.assertTrue(any("未纳入 Git index" in issue.message for issue in result.issues))

    def test_tracked_scope_also_requires_resources_and_maintenance_handbook_in_index(self) -> None:
        self.skill("skills/example", body="[Resource](asset.md)\n")
        self.write("skills/example/asset.md", "# Asset\n")
        self.index("skills/example")
        inventory = checker.make_inventory(self.root).files - {"skills/example/asset.md", "docs/maintenance.md"}
        result = self.check(tracked=True, file_inventory=inventory)
        self.assertTrue(any(issue.path == "docs/maintenance.md" and issue.code == "required-file" for issue in result.issues))
        self.assertTrue(any(issue.code == "link-target" and "asset.md" in issue.message for issue in result.issues))

    def test_tracked_scope_requires_executable_maintenance_facilities(self) -> None:
        missing = {"scripts/prepare_release.py", "tests/test_prepare_release.py", ".github/workflows/repository-check.yml", ".github/ISSUE_TEMPLATE/bug_report.yml"}
        inventory = checker.make_inventory(self.root).files - missing
        result = self.check(tracked=True, file_inventory=inventory)
        found = {issue.path for issue in result.issues if issue.code == "required-file"}
        self.assertEqual(found, missing)

    def test_deleted_working_tree_file_is_not_available_even_when_in_index(self) -> None:
        self.skill("skills/example")
        self.index("skills/example")
        inventory = checker.make_inventory(self.root).files
        (self.root / "skills/example/README.md").unlink()
        result = self.check(tracked=True, file_inventory=inventory)
        self.assertTrue({"required-file", "read-file", "link-target"} <= set(self.codes(result)))

    def test_link_paths_are_case_sensitive_even_on_windows(self) -> None:
        self.skill("skills/example", body="[Read](readme.md)\n")
        self.index("skills/example")
        self.assertIn("link-target", self.codes(self.check()))

    def test_index_inventory_uses_nul_separated_git_paths(self) -> None:
        completed = subprocess.CompletedProcess([], 0, b"README.md\0skills/example/SKILL.md\0", b"")
        with patch.object(checker.subprocess, "run", return_value=completed) as run:
            inventory = checker.make_inventory(self.root, tracked=True)
        self.assertEqual(inventory.files, {"README.md", "skills/example/SKILL.md"})
        self.assertIn("skills/example", inventory.directories)
        self.assertEqual(run.call_args.args[0], ["git", "-C", str(self.root), "ls-files", "--cached", "-z"])

    def test_cli_runs_from_an_unrelated_working_directory(self) -> None:
        self.skill("skills/example")
        self.index("skills/example")
        with tempfile.TemporaryDirectory() as other_directory:
            completed = subprocess.run(
                [sys.executable, "-B", str(SCRIPT), "--root", str(self.root)],
                cwd=other_directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", errors="replace",
            )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("1", completed.stdout)


if __name__ == "__main__":
    unittest.main()
