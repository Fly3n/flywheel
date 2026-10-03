"""Release safety conditions with isolated data and no external writes."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_release.py"
spec = importlib.util.spec_from_file_location("prepare_release", SCRIPT)
assert spec is not None and spec.loader is not None
release = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = release
spec.loader.exec_module(release)


class VersionTests(unittest.TestCase):
    def test_accepts_one_canonical_tag_for_prefixed_and_plain_input(self):
        self.assertEqual(release.normalize_version("0.1.0"), "v0.1.0")
        self.assertEqual(release.normalize_version("v0.1.0"), "v0.1.0")

    def test_rejects_ambiguous_versions_and_git_patterns(self):
        for value in ("v0.1", "01.2.3", "v0.1.0*", "../v0.1.0", "v1.0.0-beta", "v1.2.03"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                release.normalize_version(value)


class MaintenanceNotesTests(unittest.TestCase):
    def test_preview_can_use_unreleased_but_it_is_not_dated(self):
        notes = release.select_notes("## [Unreleased]\n\n### 新增\n- 反馈入口\n", "v0.1.0", True)
        self.assertEqual(notes.section, "Unreleased")
        self.assertFalse(notes.dated)

    def test_strict_mode_does_not_publish_unreleased_as_a_version(self):
        with self.assertRaises(ValueError):
            release.select_notes("## [Unreleased]\n- 反馈入口\n", "v0.1.0", False)

    def test_selects_only_the_target_version_not_the_next_section(self):
        content = "## [Unreleased]\n- 将来的变更\n\n## [v0.1.0] - 2026-10-01\n- 初次版本\n\n## [v0.0.1] - 2026-09-01\n- 旧版本\n"
        notes = release.select_notes(content, "v0.1.0", False)
        self.assertTrue(notes.dated)
        self.assertEqual(notes.body, "- 初次版本")

    def test_handbook_process_and_examples_are_excluded_from_release_notes(self):
        content = """# 维护手册

## 维护流程
- 先检查仓库结构。
```md
## [v0.1.0] - 2026-10-01
- 格式示例
```

## [Unreleased]
### 新增
- 实际未发布变更

## 反馈流程
- 提供复现信息。

## [v0.1.0] - 2026-10-01
- 实际版本变更

# 附录
- 附录说明。
"""
        preview = release.select_notes(content, "v0.2.0", True)
        self.assertEqual(preview.body, "### 新增\n- 实际未发布变更")
        version = release.select_notes(content, "v0.1.0", False)
        self.assertEqual(version.body, "- 实际版本变更")
        self.assertTrue(version.dated)

    def test_process_bullets_after_empty_version_are_not_release_evidence(self):
        content = "## [v0.1.0] - 2026-10-01\n### 新增\n\n## 维护流程\n- 检查仓库。\n"
        with self.assertRaisesRegex(ValueError, "docs/maintenance.md.*没有实际变更条目"):
            release.select_notes(content, "v0.1.0", False)

    def test_empty_or_undated_release_is_blocked(self):
        for content in ("## [v0.1.0]\n- 新增\n", "## [v0.1.0] - 2026-10-01\n### 新增\n", "## [v0.1.0] - 2026-02-30\n- 新增\n"):
            with self.subTest(content=content), self.assertRaises(ValueError):
                release.select_notes(content, "v0.1.0", False)

    def test_duplicate_version_sections_are_not_silently_selected(self):
        for content in ("## [v0.1.0] - 2026-10-01\n- 一\n## [v0.1.0] - 2026-10-01\n- 二\n", "## [v0.1.0] - 2026-10-01\n- 一\n## [0.1.0] - 2026-10-01\n- 二\n"):
            with self.subTest(content=content), self.assertRaises(ValueError):
                release.select_notes(content, "v0.1.0", False)

    def test_examples_and_comments_are_not_release_evidence(self):
        for content in (
            "```md\n## [v0.1.0] - 2026-10-01\n- 示例\n```\n",
            "~~~md\n## [v0.1.0] - 2026-10-01\n- 示例\n~~~\n",
            "## [v0.1.0] - 2026-10-01\n<!--\n- 隐藏草稿\n-->\n",
            "## [v0.1.0] - 2026-10-01\n```\n- 示例\n```\n",
            "## [v0.1.0] - 2026-10-01\n    - 缩进代码示例\n",
        ):
            with self.subTest(content=content), self.assertRaises(ValueError):
                release.select_notes(content, "v0.1.0", False)

    def test_real_notes_can_include_code_examples_without_creating_versions(self):
        content = "## [v0.1.0] - 2026-10-01\n- 提供说明示例\n```md\n## [v9.9.9] - 2026-10-01\n- 示例版本\n```\n"
        notes = release.select_notes(content, "v0.1.0", False)
        self.assertIn("说明示例", notes.body)
        self.assertIn("v9.9.9", notes.body)

    def test_literal_comment_markers_in_code_do_not_hide_real_notes(self):
        for body in (
            "```html\n<!--\n```\n- 真实变更\n",
            "    <!--\n- 真实变更\n",
            "- `<!--` 标记的真实说明\n",
            "<!--\n```md\n-->\n- 真实变更\n",
        ):
            content = "## [v0.1.0] - 2026-10-01\n" + body
            with self.subTest(body=body):
                self.assertTrue(release.select_notes(content, "v0.1.0", False).dated)


class PreflightTests(unittest.TestCase):
    def runner(self, *, branch="main", dirty="", tag="", structure=0, tests=0, test_count=1):
        calls = []

        def fake(root, command):
            calls.append(command)
            if command[:2] == ["git", "symbolic-ref"]:
                code, output = 0, branch
            elif command[:2] == ["git", "status"]:
                code, output = 0, dirty
            elif command[:2] == ["git", "tag"]:
                code, output = 0, tag
            elif command[:2] == ["git", "rev-parse"]:
                code, output = 0, "abc123"
            elif command[-1] == "--tracked":
                code, output = structure, "structure result"
            else:
                code, output = tests, f"Ran {test_count} tests in 0.01s\n\nOK\n"
            return subprocess.CompletedProcess(command, code, output, "")

        return fake, calls

    def checks(self, **kwargs):
        runner, calls = self.runner(**kwargs)
        checks, head = release.collect_checks(Path("repository"), "v0.1.0", release.Notes("- 新增", "v0.1.0", True), runner)
        return {check.name: check for check in checks}, head, calls

    def test_clean_main_with_all_checks_passes_local_preflight(self):
        checks, head, calls = self.checks()
        self.assertTrue(all(check.passed for check in checks.values()))
        self.assertEqual(head, "abc123")
        self.assertFalse(any("push" in command or "release" in command for command in calls))

    def test_dirty_or_untracked_files_block_a_release(self):
        checks, _, _ = self.checks(dirty="?? new-file.md\n M README.md")
        self.assertFalse(checks["干净提交"].passed)

    def test_hidden_untracked_git_status_setting_cannot_make_a_release_clean(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(root), "config", "status.showUntrackedFiles", "no"], check=True, capture_output=True)
            (root / "untracked.md").write_text("unreleased content", encoding="utf-8")
            fake, _ = self.runner()

            def runner(path, command):
                if command[:2] == ["git", "status"]:
                    return subprocess.run(command, cwd=path, capture_output=True, text=True, encoding="utf-8")
                return fake(path, command)

            checks, _ = release.collect_checks(root, "v0.1.0", release.Notes("- 新增", "v0.1.0", True), runner)
            clean = next(check for check in checks if check.name == "干净提交")
            self.assertFalse(clean.passed)
            self.assertIn("untracked.md", clean.detail)

    def test_non_main_branch_and_existing_tag_block_a_release(self):
        checks, _, _ = self.checks(branch="codex/work", tag="v0.1.0")
        self.assertFalse(checks["发布分支"].passed)
        self.assertFalse(checks["本地 tag"].passed)

    def test_repository_or_test_failure_is_not_reported_as_ready(self):
        checks, _, _ = self.checks(structure=1, tests=1)
        self.assertFalse(checks["待发布文件结构"].passed)
        self.assertFalse(checks["维护脚本测试"].passed)

    def test_zero_test_success_exit_code_is_not_test_evidence(self):
        checks, _, _ = self.checks(test_count=0)
        self.assertFalse(checks["维护脚本测试"].passed)

    def test_unreleased_preview_still_has_a_release_blocker(self):
        runner, _ = self.runner()
        checks, head = release.collect_checks(Path("repository"), "v0.1.0", release.Notes("- 新增", "Unreleased", False), runner)
        self.assertFalse(checks[0].passed)
        output = release.render_notes("v0.1.0", release.Notes("- 新增", "Unreleased", False), checks, head, True)
        self.assertIn("1 项本地发布阻塞", output)
        self.assertIn("尚未发布", output)


if __name__ == "__main__":
    unittest.main()
