#!/usr/bin/env python3
"""Check repository structure with Python 3.11+ and the standard library.

The checker reads current working-tree contents in both modes. ``--tracked``
limits available paths to the Git index; it does not read staged file contents.
It never imports or executes capability scripts and never accesses the network.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import posixpath
import re
import subprocess
import sys
from typing import Iterable
from urllib.parse import unquote


REQUIRED_DOCUMENTS = (
    "AGENTS.md", "README.md", "docs/maintenance.md",
)
REQUIRED_MAINTENANCE_FILES = (
    "scripts/check_repository.py", "scripts/prepare_release.py",
    "tests/test_check_repository.py", "tests/test_prepare_release.py",
    ".github/workflows/repository-check.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/usage_feedback.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/PULL_REQUEST_TEMPLATE.md", ".github/release.yml",
)
IGNORED_DIRECTORIES = {
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".idea", ".vscode",
    ".workbuddy-ai",
}
INFRASTRUCTURE_DIRECTORIES = {"scripts", "tests", "docs", "skills"}
NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
URI_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


@dataclass(frozen=True)
class Issue:
    code: str
    path: str
    message: str
    line: int | None = None

    def __str__(self) -> str:
        location = self.path + (f":{self.line}" if self.line else "")
        return f"[{self.code}] {location}: {self.message}"


@dataclass(frozen=True)
class MarkdownLink:
    target: str | None
    line: int
    reference: str | None = None


@dataclass
class Inventory:
    root: Path
    files: set[str]
    directories: set[str]
    tracked: bool

    def contains(self, path: str) -> bool:
        return path in self.files or path in self.directories

    def available(self, path: str) -> bool:
        return self.contains(path) and (self.root / path).exists()

    def unavailable_reason(self, path: str) -> str:
        if self.tracked and not self.contains(path) and (self.root / path).exists():
            return "工作区存在，但未纳入 Git index（尚未跟踪）"
        if self.contains(path):
            return "已纳入检查范围，但当前工作区文件或目录缺失"
        return "目标不存在或未纳入检查范围（路径区分大小写）"


@dataclass
class CheckResult:
    issues: list[Issue]
    capabilities: set[str]
    markdown_files: int


def make_inventory(
    root: Path, *, tracked: bool = False, file_inventory: Iterable[str] | None = None,
) -> Inventory:
    """An injected inventory lets tests model the index without touching Git."""
    directories: set[str] = {"."}
    if file_inventory is not None:
        files = {str(path).replace("\\", "/").removeprefix("./") for path in file_inventory}
    elif tracked:
        process = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "-z"],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if process.returncode:
            detail = process.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"无法读取 Git index：{detail}")
        files = {os.fsdecode(path) for path in process.stdout.split(b"\0") if path}
    else:
        files = set()
        for directory, child_directories, child_files in os.walk(root):
            child_directories[:] = sorted(
                name for name in child_directories if name not in IGNORED_DIRECTORIES
            )
            relative_directory = Path(directory).relative_to(root).as_posix()
            directories.add(relative_directory)
            files.update(
                (Path(directory) / name).relative_to(root).as_posix()
                for name in child_files
            )
    for filename in files:
        parent = posixpath.dirname(filename)
        while parent:
            directories.add(parent)
            parent = posixpath.dirname(parent)
    return Inventory(root, files, directories, tracked)


def _mask_markdown(text: str) -> str:
    """Hide code, frontmatter and HTML comments while preserving line numbers."""
    text = re.sub(r"<!--.*?-->", lambda match: re.sub(r"[^\n]", " ", match[0]), text, flags=re.S)
    text = re.sub(
        r"<(code|pre|script|style)\b[^>]*>.*?</\1\s*>",
        lambda match: re.sub(r"[^\n]", " ", match[0]), text, flags=re.S | re.I,
    )
    lines = text.splitlines(keepends=True)
    result = []
    fence: tuple[str, int] | None = None
    frontmatter = bool(lines and lines[0].strip().lstrip("\ufeff") == "---")
    for number, line in enumerate(lines):
        blank = re.sub(r"[^\n]", " ", line)
        if frontmatter:
            result.append(blank)
            if number and line.strip() == "---":
                frontmatter = False
            continue
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            result.append(blank)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not marker[2].strip():
                fence = None
            continue
        if marker and (marker[1][0] != "`" or "`" not in marker[2]):
            fence = (marker[1][0], len(marker[1]))
            result.append(blank)
        elif line.startswith(("    ", "\t")):
            result.append(blank)
        else:
            result.append(re.sub(r"(`+)(.*?)\1", lambda match: " " * len(match[0]), line))
    return "".join(result)


def _escaped(text: str, position: int) -> bool:
    count = 0
    while position > 0 and text[position - 1] == "\\":
        count += 1
        position -= 1
    return count % 2 == 1


def _closing_bracket(text: str, start: int) -> int | None:
    depth = 0
    for position in range(start, len(text)):
        if _escaped(text, position):
            continue
        if text[position] == "[":
            depth += 1
        elif text[position] == "]":
            depth -= 1
            if depth == 0:
                return position
        elif text[position] == "\n" and depth == 1:
            return None
    return None


def _inline_destination(text: str, start: int) -> tuple[str, int] | None:
    position = start + 1
    while position < len(text) and text[position].isspace():
        position += 1
    target_start = position
    if position < len(text) and text[position] == "<":
        position += 1
        target_start = position
        while position < len(text) and (text[position] != ">" or _escaped(text, position)):
            if text[position] == "\n":
                return None
            position += 1
        if position == len(text):
            return None
        target = text[target_start:position]
        position += 1
    else:
        depth = 0
        while position < len(text):
            character = text[position]
            if not _escaped(text, position):
                if character == "(":
                    depth += 1
                elif character == ")":
                    if depth == 0:
                        return text[target_start:position], position + 1
                    depth -= 1
                elif character.isspace() and depth == 0:
                    break
            position += 1
        target = text[target_start:position]
    # A destination can be followed by an optional Markdown title.
    tail = re.match(r"\s*(?:\"[^\"]*\"|'[^']*'|\([^()]*\))?\s*\)", text[position:])
    return (target, position + tail.end()) if tail else None


def _reference_key(label: str) -> str:
    return " ".join(label.split()).casefold()


def markdown_links(text: str) -> list[MarkdownLink]:
    masked = _mask_markdown(text)
    references: dict[str, str] = {}
    links: list[MarkdownLink] = []
    definition = re.compile(r"^ {0,3}\[([^]\n]+)\]:[ \t]*(<[^>\n]*>|\S+)(?:[ \t]+.*)?$", re.M)
    for match in definition.finditer(masked):
        target = match[2]
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        references.setdefault(_reference_key(match[1]), target)
        links.append(MarkdownLink(target, masked.count("\n", 0, match.start()) + 1))
    masked = definition.sub(lambda match: " " * len(match[0]), masked)
    position = 0
    while position < len(masked):
        if masked[position] != "[" or _escaped(masked, position):
            position += 1
            continue
        end = _closing_bracket(masked, position)
        if end is None:
            position += 1
            continue
        line = masked.count("\n", 0, position) + 1
        after = end + 1
        if after < len(masked) and masked[after] == "(":
            destination = _inline_destination(masked, after)
            if destination:
                target, position = destination
                links.append(MarkdownLink(target, line))
                continue
        if after < len(masked) and masked[after] == "[":
            reference_end = _closing_bracket(masked, after)
            if reference_end is not None:
                label = masked[after + 1:reference_end] or masked[position + 1:end]
                key = _reference_key(label)
                links.append(MarkdownLink(references.get(key), line, label))
                position = reference_end + 1
                continue
        key = _reference_key(masked[position + 1:end])
        if key in references:
            links.append(MarkdownLink(references[key], line, key))
        position = end + 1
    return links


def _local_target(source: str, target: str) -> tuple[str | None, str | None]:
    target = re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~])", r"\1", target).strip()
    if not target or target.startswith(("#", "//")):
        return None, None
    if re.match(r"^[a-zA-Z]:[\\/]", target) or target.startswith("/"):
        return None, "本地链接应使用仓库内的相对路径"
    if URI_PATTERN.match(target):
        return None, None
    path = unquote(target.split("#", 1)[0].split("?", 1)[0]).replace("\\", "/")
    # Template variables describe a future path, not a declared concrete file.
    if any(marker in path for marker in ("<", ">", "${", "$(")) or re.search(r"\{[^{}]*\}", path) or "..." in path.split("/"):
        return None, None
    relative = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
    if relative == ".." or relative.startswith("../"):
        return None, "相对链接越过仓库根目录"
    return relative, None


def _string_scalar(value: str) -> str | None:
    value = value.strip()
    if value.startswith('"'):
        try:
            parsed, end = json.JSONDecoder().raw_decode(value)
        except ValueError:
            return None
        remainder = value[end:].strip()
        return parsed if isinstance(parsed, str) and (not remainder or remainder.startswith("#")) else None
    if value.startswith("'"):
        match = re.fullmatch(r"'((?:[^']|'')*)'\s*(?:#.*)?", value)
        return match[1].replace("''", "'") if match else None
    value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    if not value or value.startswith(("[", "{", "&", "*", "!", "|", ">")):
        return None
    if value.lower() in {"null", "~", "true", "false"} or re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value):
        return None
    return value


def check_frontmatter(path: str, text: str) -> list[Issue]:
    issues = []
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0] != "---":
        return [Issue("frontmatter", path, "SKILL.md 必须以 --- 包围的 frontmatter 开头", 1)]
    end = next((number for number in range(1, len(lines)) if lines[number] == "---"), None)
    if end is None:
        return [Issue("frontmatter", path, "frontmatter 缺少结束的 ---", 1)]
    fields: dict[str, tuple[str | None, int]] = {}
    for number in range(1, end):
        match = re.match(r"^(name|description):\s*(.*)$", lines[number])
        if not match:
            continue
        key, value = match.groups()
        if key in fields:
            issues.append(Issue("frontmatter", path, f"frontmatter 重复声明 {key}", number + 1))
            continue
        if re.fullmatch(r"[|>][+-]?(?:[ \t]+#.*)?", value.strip()):
            block = []
            for following in range(number + 1, end):
                line = lines[following]
                if line and not line[0].isspace():
                    break
                block.append(line.strip())
            parsed = " ".join(block).strip()
        else:
            parsed = _string_scalar(value)
        fields[key] = (parsed, number + 1)
    for key in ("name", "description"):
        value, line = fields.get(key, (None, 1))
        if not value or not value.strip():
            issues.append(Issue("frontmatter", path, f"frontmatter 的 {key} 必须是非空字符串", line))
    name, line = fields.get("name", (None, 1))
    expected = posixpath.basename(posixpath.dirname(path))
    if name and name != expected:
        issues.append(Issue("frontmatter-name", path, f"name 应与所在目录一致：{expected!r}，实际为 {name!r}", line))
    return issues


def _table_cells(line: str) -> list[str] | None:
    line = line.strip()
    if "|" not in line:
        return None
    cells = re.split(r"(?<!\\)\|", line)
    if line.startswith("|"):
        cells = cells[1:]
    if line.endswith("|") and not _escaped(line, len(line) - 1):
        cells = cells[:-1]
    return [cell.strip() for cell in cells]


def check_index(text: str, inventory: Inventory, capabilities: set[str]) -> list[Issue]:
    issues = []
    lines = _mask_markdown(text).splitlines()
    tables = [number for number, line in enumerate(lines) if _table_cells(line) == ["能力", "解决什么问题"]]
    if len(tables) != 1:
        return [Issue("index-table", "README.md", "必须有且只有一张表头为“能力 | 解决什么问题”的两列表")]
    header = tables[0]
    separator = _table_cells(lines[header + 1]) if header + 1 < len(lines) else None
    if separator is None or len(separator) != 2 or any(not re.fullmatch(r":?-{3,}:?", cell) for cell in separator):
        return [Issue("index-table", "README.md", "能力索引缺少合法的两列表分隔行", header + 2)]
    indexed = set()
    for number in range(header + 2, len(lines)):
        cells = _table_cells(lines[number])
        if cells is None:
            break
        if len(cells) != 2 or not all(cells):
            issues.append(Issue("index-table", "README.md", "每个能力索引行必须有两个非空单元格", number + 1))
            continue
        # Definitions may be elsewhere in README; use them for reference links.
        definitions = "\n".join(line for line in lines if re.match(r"^ {0,3}\[[^]]+\]:", line))
        links = markdown_links(cells[0] + "\n" + definitions)
        links = [link for link in links if link.line == 1]
        local = []
        for link in links:
            if link.target is None:
                continue
            path, error = _local_target("README.md", link.target)
            if path and not error:
                local.append(path.rstrip("/"))
        if len(local) != 1:
            issues.append(Issue("index-target", "README.md", "能力单元格必须包含一个具体的本地能力链接", number + 1))
            continue
        path = local[0]
        capability = path.removesuffix("/README.md")
        if capability not in capabilities:
            reason = inventory.unavailable_reason(path) if not inventory.available(path) else "目标不是已识别的能力目录或其 README.md"
            issues.append(Issue("index-target", "README.md", f"索引目标 {path!r} 不对应检查范围内的能力：{reason}", number + 1))
            continue
        if capability in indexed:
            issues.append(Issue("index-duplicate", "README.md", f"能力 {capability!r} 在索引中重复出现", number + 1))
        indexed.add(capability)
    for capability in sorted(capabilities - indexed):
        issues.append(Issue("index-missing", "README.md", f"能力 {capability!r} 尚未列入索引"))
    return issues


def check_repository(
    root: Path, *, tracked: bool = False, file_inventory: Iterable[str] | None = None,
) -> CheckResult:
    root = root.resolve()
    inventory = make_inventory(root, tracked=tracked, file_inventory=file_inventory)
    issues: list[Issue] = []

    def require(path: str) -> None:
        if path not in inventory.files or not (root / path).is_file():
            issues.append(Issue("required-file", path, f"缺少必需文件：{inventory.unavailable_reason(path)}"))

    for path in REQUIRED_DOCUMENTS + REQUIRED_MAINTENANCE_FILES:
        require(path)
    for path in sorted(inventory.files):
        if "/" not in path and path.lower().endswith(".md") and path not in {"AGENTS.md", "README.md"}:
            issues.append(Issue("root-markdown", path, "根目录只保留 AGENTS.md 和 README.md；维护说明放入 docs/maintenance.md"))
    standalone = {
        path for path in inventory.directories
        if path.startswith("skills/") and path.count("/") == 1
    }
    suites = {
        path.split("/", 1)[0] for path in inventory.files
        if "/" in path and path.endswith("/SKILL.md")
        and path.split("/", 1)[0] not in INFRASTRUCTURE_DIRECTORIES
        and not path.startswith(".")
    }
    capabilities = standalone | suites
    for capability in sorted(capabilities):
        name = posixpath.basename(capability)
        if not NAME_PATTERN.fullmatch(name):
            issues.append(Issue("directory-name", capability, "能力目录名必须由小写字母、数字和连字符组成，各段不能为空"))
        require(f"{capability}/README.md")
        if capability in standalone:
            require(f"{capability}/SKILL.md")
        elif not any(path.startswith(capability + "/") and path.count("/") >= 2 and path.endswith("/SKILL.md") for path in inventory.files):
            issues.append(Issue("suite-units", capability, "成套组件至少需要一个子目录中的 SKILL.md"))
    skill_files = sorted(path for path in inventory.files if path == "SKILL.md" or path.endswith("/SKILL.md"))
    for path in skill_files:
        parent_name = posixpath.basename(posixpath.dirname(path))
        if not NAME_PATTERN.fullmatch(parent_name):
            issues.append(Issue("directory-name", posixpath.dirname(path), "SKILL.md 所在目录名必须使用小写字母、数字和连字符"))
        if not any(path.startswith(capability + "/") for capability in capabilities):
            issues.append(Issue("skill-location", path, "SKILL.md 必须位于 skills/<name>/ 或根目录组件内"))
    markdown_files = sorted(path for path in inventory.files if path.lower().endswith(".md"))
    for path in markdown_files:
        try:
            text = (root / path).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as error:
            issues.append(Issue("read-file", path, f"无法读取 UTF-8 文档：{error}"))
            continue
        if path in skill_files:
            issues.extend(check_frontmatter(path, text))
        if path == "README.md":
            issues.extend(check_index(text, inventory, capabilities))
        owner = next((capability for capability in capabilities if path.startswith(capability + "/")), None)
        for link in markdown_links(text):
            if link.target is None:
                issues.append(Issue("link-reference", path, f"Markdown 引用 {link.reference!r} 未定义", link.line))
                continue
            target, error = _local_target(path, link.target)
            if error:
                issues.append(Issue("link-path", path, error, link.line))
                continue
            if target is None:
                continue
            try:
                resolved = (root / target).resolve()
                resolved.relative_to(root)
                if owner:
                    resolved.relative_to((root / owner).resolve())
            except (ValueError, OSError):
                boundary = f"能力目录 {owner!r}" if owner else "仓库根目录"
                issues.append(Issue("link-boundary", path, f"链接 {link.target!r} 越过{boundary}，能力应可独立拿走", link.line))
                continue
            if not inventory.available(target):
                issues.append(Issue("link-target", path, f"链接 {link.target!r} 无效：{inventory.unavailable_reason(target)}", link.line))
    return CheckResult(issues, capabilities, len(markdown_files))


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="检查仓库结构、声明的本地 Markdown 链接及能力索引（Python 3.11+，无第三方依赖）。")
    parser.add_argument("--tracked", action="store_true", help="仅以 Git index 中的路径判断可发布内容；读取当前工作区文档")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="仓库根目录；默认从脚本位置确定，不受当前工作目录影响")
    arguments = parser.parse_args(argv)
    print("范围：Git index 中的路径；文档读取当前工作区内容。" if arguments.tracked else "范围：当前工作区（包括未跟踪内容，略过常见生成目录）。")
    print("此检查只证明结构、声明的本地 Markdown 链接和索引一致性；不验证工具行为、隐私或授权。")
    try:
        result = check_repository(arguments.root, tracked=arguments.tracked)
    except (OSError, RuntimeError) as error:
        print(f"[inventory-error] {error}")
        return 2
    for issue in result.issues:
        print(issue)
    if result.issues:
        print(f"未通过：{len(result.issues)} 项问题；检查 {len(result.capabilities)} 个能力、{result.markdown_files} 个 Markdown 文件。")
        return 1
    print(f"通过：{len(result.capabilities)} 个能力、{result.markdown_files} 个 Markdown 文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
