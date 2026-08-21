"""Workspace installation lifecycle for FOCUS-packaged managed skills."""

from __future__ import annotations

import filecmp
import importlib
import pathlib
import shutil
from dataclasses import dataclass


_MANAGED_SKILL_MARKER = ".focus-managed"


@dataclass(frozen=True, slots=True)
class _ManagedSkillSpec:
    name: str
    package: str


_MANAGED_SKILLS: tuple[_ManagedSkillSpec, ...] = (
    _ManagedSkillSpec(
        name="feishu-send-image", package="bot.managed_skills.feishu_send_image"
    ),
    _ManagedSkillSpec(
        name="feishu-scheduled-prompts",
        package="bot.managed_skills.feishu_scheduled_prompts",
    ),
)
_DEFAULT_MANAGED_SKILL_NAME = _MANAGED_SKILLS[0].name


def _managed_skill_spec(skill_name: str) -> _ManagedSkillSpec:
    normalized = str(skill_name or "").strip()
    for spec in _MANAGED_SKILLS:
        if spec.name == normalized:
            return spec
    raise ValueError(f"未知受管 skill：{normalized}")


def _managed_skill_source_dir(
    skill_name: str = _DEFAULT_MANAGED_SKILL_NAME,
) -> pathlib.Path:
    package = importlib.import_module(_managed_skill_spec(skill_name).package)
    return pathlib.Path(package.__file__).resolve().parent / "skill"


def _managed_skill_target_dir(
    skill_name: str = _DEFAULT_MANAGED_SKILL_NAME,
) -> pathlib.Path:
    return pathlib.Path.cwd() / ".agents" / "skills" / skill_name


def _managed_skill_marker_path(skill_dir: pathlib.Path) -> pathlib.Path:
    return skill_dir / _MANAGED_SKILL_MARKER


def _write_managed_skill_marker(skill_dir: pathlib.Path) -> None:
    skill_name = pathlib.Path(skill_dir).name
    _managed_skill_marker_path(skill_dir).write_text(
        f"managed_by=focus\nskill={skill_name}\n",
        encoding="utf-8",
    )


def _is_focus_managed_skill(skill_dir: pathlib.Path) -> bool:
    marker = _managed_skill_marker_path(skill_dir)
    if not marker.exists():
        return False
    try:
        contents = marker.read_text(encoding="utf-8")
    except OSError:
        return False
    skill_name = pathlib.Path(skill_dir).name
    return "managed_by=focus" in contents and f"skill={skill_name}" in contents


def _skill_tree_matches_source(
    skill_dir: pathlib.Path, source_dir: pathlib.Path
) -> bool:
    normalized_target = pathlib.Path(skill_dir)
    normalized_source = pathlib.Path(source_dir)
    if not normalized_target.is_dir() or not normalized_source.is_dir():
        return False
    comparison = filecmp.dircmp(
        normalized_source,
        normalized_target,
        ignore=[_MANAGED_SKILL_MARKER, "__pycache__"],
    )
    if comparison.left_only or comparison.right_only or comparison.funny_files:
        return False
    _, mismatch, errors = filecmp.cmpfiles(
        normalized_source,
        normalized_target,
        comparison.common_files,
        shallow=False,
    )
    if mismatch or errors:
        return False
    return all(
        _skill_tree_matches_source(
            normalized_target / common_dir, normalized_source / common_dir
        )
        for common_dir in comparison.common_dirs
    )


def _handle_skill_install() -> int:
    target_parent = pathlib.Path.cwd() / ".agents" / "skills"
    target_parent.mkdir(parents=True, exist_ok=True)
    install_plan: list[tuple[_ManagedSkillSpec, pathlib.Path, pathlib.Path, str]] = []
    for spec in _MANAGED_SKILLS:
        source_dir = _managed_skill_source_dir(spec.name)
        if not source_dir.is_dir():
            raise ValueError(f"skill 源目录不存在：{source_dir}")
        target_dir = _managed_skill_target_dir(spec.name)
        action = "copy"
        if target_dir.exists():
            if not target_dir.is_dir():
                raise ValueError(f"skill 目标路径已存在且不是目录：{target_dir}")
            if not _is_focus_managed_skill(target_dir):
                if _skill_tree_matches_source(target_dir, source_dir):
                    action = "keep"
                else:
                    raise ValueError(
                        "目标 skill 已存在且不是 FOCUS 受管安装；"
                        f"请先手动处理：{target_dir}"
                    )
        install_plan.append((spec, source_dir, target_dir, action))

    for spec, source_dir, target_dir, action in install_plan:
        if action == "keep":
            print(f"当前目录已可用 skill: {spec.name}")
            print(f"target: {target_dir}")
            continue
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(source_dir, target_dir)
        _write_managed_skill_marker(target_dir)
        print(f"已安装 skill: {spec.name}")
        print(f"source: {source_dir}")
        print(f"target: {target_dir}")
    return 0


def _handle_skill_uninstall() -> int:
    removed_any = False
    for spec in _MANAGED_SKILLS:
        target_dir = _managed_skill_target_dir(spec.name)
        if not target_dir.exists():
            print(f"未安装 skill: {spec.name}")
            print(f"target: {target_dir}")
            continue
        if not target_dir.is_dir():
            raise ValueError(f"skill 目标路径不是目录：{target_dir}")
        if not _is_focus_managed_skill(target_dir):
            raise ValueError(f"目标 skill 不是 FOCUS 受管安装；拒绝删除： {target_dir}")
        shutil.rmtree(target_dir)
        removed_any = True
        print(f"已卸载 skill: {spec.name}")
        print(f"target: {target_dir}")
    if not removed_any:
        print("当前目录没有 FOCUS 受管安装的 skill。")
    return 0
