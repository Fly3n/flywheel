#!/usr/bin/env python3
"""Read-only local release checks and Markdown notes; never create tags or releases."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class Notes:
    body: str
    section: str
    dated: bool


def normalize_version(value: str) -> str:
    value = value.removeprefix("v")
    if not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", value):
        raise ValueError("版本应为 v0.1.0 或 0.1.0 形式的三段数字，不能带前导零。")
    return "v" + value


def visible_markdown(content: str) -> str:
    """Mask comments and code without changing offsets into the original notes."""
    masked = []
    fence: tuple[str, int] | None = None
    comment = False
    for line in content.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not marker[2].strip():
                fence = None
            masked.append(re.sub(r"[^\n]", " ", line))
            continue
        if not comment and marker and (marker[1][0] != "`" or "`" not in marker[2]):
            fence = (marker[1][0], len(marker[1]))
            masked.append(re.sub(r"[^\n]", " ", line))
            continue
        if not comment and line.startswith(("    ", "\t")):
            masked.append(re.sub(r"[^\n]", " ", line))
            continue
        # Literal comment markers inside inline code cannot start a comment.
        searchable = re.sub(r"(`+)(.*?)\1", lambda match: " " * len(match[0]), line)
        characters = list(line)
        cursor = 0
        while cursor < len(line):
            if comment:
                closing = line.find("-->", cursor)
                end = len(line) if closing < 0 else closing + 3
                characters[cursor:end] = ["\n" if char == "\n" else " " for char in line[cursor:end]]
                cursor = end
                if closing < 0:
                    break
                comment = False
            else:
                opening = searchable.find("<!--", cursor)
                if opening < 0:
                    break
                comment = True
                cursor = opening
        masked.append("".join(characters))
    return "".join(masked)


def select_notes(content: str, tag: str, preview: bool) -> Notes:
    visible = visible_markdown(content)
    headings = list(re.finditer(r"^##\s+\[([^\]]+)\]([^\n]*)$", visible, re.MULTILINE))
    sections: dict[str, tuple[str, str]] = {}
    for heading in headings:
        name = heading.group(1)
        if name in sections:
            raise ValueError(f"docs/maintenance.md 中有重复版本段：{name}")
        following = re.search(r"^#{1,2}\s+", visible[heading.end():], re.MULTILINE)
        end = heading.end() + following.start() if following else len(content)
        sections[name] = (content[heading.end():end], heading.group(2).strip())

    target = next((name for name in (tag, tag[1:]) if name in sections), None)
    if tag in sections and tag[1:] in sections:
        raise ValueError(f"docs/maintenance.md 中有两份 {tag} 版本段，请保留一份。")
    if target is None:
        if not preview or "Unreleased" not in sections:
            raise ValueError(f"docs/maintenance.md 缺少 {tag} 版本段；准备阶段可使用 --preview 读取 Unreleased。")
        target = "Unreleased"

    body, suffix = sections[target]
    if not re.search(r"^\s*[-*]\s+\S", visible_markdown(body), re.MULTILINE):
        raise ValueError(f"docs/maintenance.md 的 {target} 段没有实际变更条目。")

    dated = target != "Unreleased"
    if dated:
        match = re.fullmatch(r"-\s+(\d{4}-\d{2}-\d{2})", suffix)
        if match is None:
            raise ValueError(f"{target} 标题需要实际发布日期：## [{tag}] - YYYY-MM-DD")
        try:
            date.fromisoformat(match.group(1))
        except ValueError as error:
            raise ValueError(f"{target} 发布日期无效：{match.group(1)}") from error
    return Notes(body.strip(), target, dated)


def run_command(root: Path, command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
    )


def collect_checks(
    root: Path, tag: str, notes: Notes,
    runner: Callable[[Path, list[str]], subprocess.CompletedProcess[str]] = run_command,
) -> tuple[list[Check], str]:
    checks = [Check(
        "版本说明", notes.dated,
        f"使用 {notes.section} 段" if notes.dated else "当前使用 Unreleased 草稿，尚未整理到目标版本和日期",
    )]
    head = "无法读取"

    def execute(command: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return runner(root, command)
        except (OSError, subprocess.TimeoutExpired) as error:
            return subprocess.CompletedProcess(command, 1, "", str(error))

    branch = execute(["git", "symbolic-ref", "--quiet", "--short", "HEAD"])
    branch_name = branch.stdout.strip()
    checks.append(Check("发布分支", branch.returncode == 0 and branch_name == "main", branch_name or branch.stderr.strip() or "当前不是命名分支"))

    status = execute(["git", "status", "--porcelain", "--untracked-files=all"])
    dirty = status.stdout.strip()
    detail = "工作区与 index 干净" if status.returncode == 0 and not dirty else dirty or status.stderr.strip()
    checks.append(Check("干净提交", status.returncode == 0 and not dirty, detail))

    existing = execute(["git", "tag", "--list", tag])
    checks.append(Check("本地 tag", existing.returncode == 0 and not existing.stdout.strip(), "目标 tag 尚未在本地创建" if existing.returncode == 0 and not existing.stdout.strip() else existing.stdout.strip() or existing.stderr.strip()))

    commit = execute(["git", "rev-parse", "HEAD"])
    if commit.returncode == 0 and commit.stdout.strip():
        head = commit.stdout.strip()
    checks.append(Check("目标提交", commit.returncode == 0 and head != "无法读取", head))

    structure = execute([sys.executable, "-B", str(root / "scripts" / "check_repository.py"), "--tracked"])
    checks.append(Check("待发布文件结构", structure.returncode == 0, (structure.stdout + structure.stderr).strip()))

    tests = execute([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"])
    test_output = (tests.stdout + tests.stderr).strip()
    counts = re.findall(r"^Ran (\d+) tests? in ", test_output, re.MULTILINE)
    has_tests = bool(counts) and int(counts[-1]) > 0
    if not has_tests:
        test_output += "\n未发现非零测试执行汇总，不能作为测试通过的证据。"
    checks.append(Check("维护脚本测试", tests.returncode == 0 and has_tests, test_output))
    return checks, head


def render_notes(tag: str, notes: Notes, checks: list[Check], head: str, preview: bool) -> str:
    title = f"# flywheel {tag}" + ("（发布说明预览）" if preview else "")
    by_name = {check.name: check for check in checks}
    blockers = sum(not check.passed for check in checks)
    lines = [title, ""]
    if preview:
        lines.extend([f"本地工作区草稿，参考 HEAD：`{head}`。当前有 {blockers} 项本地发布阻塞，尚未发布。", ""])
    lines.extend([notes.body, "", "## 验证范围", ""])
    for name in ("待发布文件结构", "维护脚本测试"):
        state = "通过" if by_name[name].passed else "未通过"
        lines.append(f"- {name}：{state}。")
    lines.extend([
        "- 上述检查覆盖仓库约束与维护设施；各能力的实际行为以对应案例或 PR 中的证据为准。",
        "- 本地预检不查询远端 tag、GitHub CI 或在线平台状态，发布前按维护手册核对。",
        "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="目标版本，例如 v0.1.0")
    parser.add_argument("--preview", action="store_true", help="读取未发布说明并展示本地阻塞，不创建版本")
    args = parser.parse_args(argv)
    try:
        tag = normalize_version(args.version)
    except ValueError as error:
        parser.error(str(error))

    root = Path(__file__).resolve().parents[1]
    try:
        notes = select_notes((root / "docs" / "maintenance.md").read_text(encoding="utf-8-sig"), tag, args.preview)
    except (OSError, ValueError) as error:
        print(f"发布说明不可用：{error}", file=sys.stderr)
        return 1

    checks, head = collect_checks(root, tag, notes)
    for check in checks:
        print(f"[{'通过' if check.passed else '阻塞'}] {check.name}: {check.detail}", file=sys.stderr)
    blockers = [check for check in checks if not check.passed]
    if blockers and not args.preview:
        print("本地发布条件未满足。使用 --preview 可查看草稿；没有创建 tag 或 Release。", file=sys.stderr)
        return 1
    print(render_notes(tag, notes, checks, head, args.preview))
    print("只读预览已生成；不代表发布条件通过。" if args.preview else "本地预检通过；仍须核对远端状态并按授权发布。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
