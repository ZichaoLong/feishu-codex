import io
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import tomllib
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch


from bot.manage_cli.entrypoint import (
    _build_parser,
    _handle_config,
    main,
)
from bot.manage_cli.service_commands import _handle_service_action
from bot.managed_skills.workspace_lifecycle import (
    _handle_skill_install,
    _handle_skill_uninstall,
    _managed_skill_source_dir,
    _skill_tree_matches_source,
)
from bot.public_command_contract import PUBLIC_COMMAND_SPECS
from bot.version import __version__
from tests.manage_cli.support import ManageCliTestCase, REPO_ROOT


class ManageCliSkillHelpParserTests(ManageCliTestCase):
    def test_import_manage_cli_entrypoint_does_not_emit_lark_pkg_resources_warning(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "import bot.manage_cli.entrypoint"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("pkg_resources is deprecated as an API", result.stderr)

    def test_import_daemon_entry_does_not_emit_lark_pkg_resources_warning(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "import bot.__main__"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("pkg_resources is deprecated as an API", result.stderr)

    def test_top_level_help_includes_examples_and_command_descriptions(self) -> None:
        parser = _build_parser()
        rendered = parser.format_help()

        self.assertIn("FOCUS 安装与 service lifecycle 管理内部入口", rendered)
        self.assertIn("首次安装与修复都请从仓库根目录执行 `bash install.sh`", rendered)
        self.assertIn("常见流程:", rendered)
        self.assertIn("首次安装 / 修复", rendered)
        self.assertIn("bash install.sh", rendered)
        self.assertIn("autostart", rendered)
        self.assertIn("`uninstall|purge` 只清理本机安装面", rendered)
        self.assertIn("focusctl instance create corp-a", rendered)
        self.assertIn("focusctl skill install", rendered)
        self.assertIn("focusctl --instance default --instance corp-a service status", rendered)
        self.assertIn("创建、列出、删除命名实例", rendered)
        self.assertIn("查看或打开当前实例相关配置文件", rendered)
        self.assertIn("安装或卸载 FOCUS 提供的工作区 skill", rendered)
        self.assertNotIn("    install            ", rendered)
        self.assertNotIn("bootstrap-install", rendered)

    def test_top_level_version_prints_project_version(self) -> None:
        parser = _build_parser()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exc:
                parser.parse_args(["--version"])

        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(stdout.getvalue().strip(), f"focusctl {__version__}")

    def test_parser_collects_repeated_instance_flags(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["--instance", "default", "--instance", "corp-a", "status"])

        self.assertEqual(args.instance, ["default", "corp-a"])

    def test_instance_help_includes_subcommand_guidance(self) -> None:
        parser = _build_parser()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exc:
                parser.parse_args(["instance", "--help"])

        self.assertEqual(exc.exception.code, 0)
        rendered = stdout.getvalue()
        self.assertIn("实例管理", rendered)
        self.assertIn("instance commands", rendered)
        self.assertIn("create", rendered)
        self.assertIn("remove", rendered)
        self.assertIn("不接受顶层 `--instance`", rendered)

    def test_instance_list_help_describes_machine_overview(self) -> None:
        parser = _build_parser()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exc:
                parser.parse_args(["instance", "list", "--help"])

        self.assertEqual(exc.exception.code, 0)
        rendered = stdout.getvalue()
        self.assertIn("service 状态", rendered)
        self.assertIn("runtime 可用性", rendered)
        self.assertIn("app-server 摘要", rendered)

    def test_autostart_help_includes_subcommand_guidance(self) -> None:
        parser = _build_parser()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exc:
                parser.parse_args(["autostart", "--help"])

        self.assertEqual(exc.exception.code, 0)
        rendered = stdout.getvalue()
        self.assertIn("登录后自动启动", rendered)
        self.assertIn("enable", rendered)
        self.assertIn("disable", rendered)
        self.assertIn("status", rendered)

    def test_skill_help_includes_subcommand_guidance(self) -> None:
        parser = _build_parser()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exc:
                parser.parse_args(["skill", "--help"])

        self.assertEqual(exc.exception.code, 0)
        rendered = stdout.getvalue()
        self.assertIn("Skill 管理", rendered)
        self.assertIn("skill commands", rendered)
        self.assertIn("install", rendered)
        self.assertIn("uninstall", rendered)
        self.assertIn("在当前目录 `.agents/skills` 安装或卸载", rendered)
        self.assertIn("不接受顶层 `--instance`", rendered)

    def test_public_install_subcommand_is_not_available(self) -> None:
        parser = _build_parser()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exc:
                parser.parse_args(["install"])

        self.assertEqual(exc.exception.code, 2)
        self.assertIn("公开命令中已无 `install`", stderr.getvalue())
        self.assertNotIn("bootstrap-install", stderr.getvalue())

    def test_handle_skill_install_copies_packaged_skill_into_current_workspace_agents_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = pathlib.Path(tmpdir) / "workspace"
            workspace.mkdir(parents=True, exist_ok=True)
            with patch.object(pathlib.Path, "cwd", return_value=workspace):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    result = _handle_skill_install()

            self.assertEqual(result, 0)
            image_target = workspace / ".agents" / "skills" / "feishu-send-image"
            schedule_target = workspace / ".agents" / "skills" / "feishu-scheduled-prompts"
            self.assertTrue((image_target / "SKILL.md").exists())
            self.assertTrue((image_target / "agents" / "openai.yaml").exists())
            self.assertTrue((image_target / ".focus-managed").exists())
            self.assertTrue((schedule_target / "SKILL.md").exists())
            self.assertTrue((schedule_target / "agents" / "openai.yaml").exists())
            self.assertTrue((schedule_target / "scripts" / "manage_scheduled_prompt.py").exists())
            self.assertTrue((schedule_target / ".focus-managed").exists())
            rendered = stdout.getvalue()
            self.assertIn("已安装 skill: feishu-send-image", rendered)
            self.assertIn("已安装 skill: feishu-scheduled-prompts", rendered)
            self.assertIn(str(image_target), rendered)
            self.assertIn(str(schedule_target), rendered)

    def test_handle_skill_install_refuses_unmanaged_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = pathlib.Path(tmpdir) / "workspace"
            target = workspace / ".agents" / "skills" / "feishu-scheduled-prompts"
            target.mkdir(parents=True, exist_ok=True)
            (target / "SKILL.md").write_text("manual\n", encoding="utf-8")

            with patch.object(pathlib.Path, "cwd", return_value=workspace):
                with self.assertRaisesRegex(ValueError, "不是 FOCUS 受管安装"):
                    _handle_skill_install()

    def test_handle_skill_install_is_noop_when_current_workspace_already_has_same_unmanaged_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = pathlib.Path(tmpdir) / "workspace"
            source = REPO_ROOT / ".agents" / "skills" / "feishu-send-image"
            target = workspace / ".agents" / "skills" / "feishu-send-image"
            shutil.copytree(source, target)

            with patch.object(pathlib.Path, "cwd", return_value=workspace):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    result = _handle_skill_install()

            self.assertEqual(result, 0)
            self.assertFalse((target / ".focus-managed").exists())
            self.assertIn("当前目录已可用 skill: feishu-send-image", stdout.getvalue())

    def test_packaged_skill_source_matches_repo_workspace_skill(self) -> None:
        repo_skill = REPO_ROOT / ".agents" / "skills" / "feishu-send-image"

        self.assertTrue(_skill_tree_matches_source(repo_skill, _managed_skill_source_dir()))

    def test_packaged_scheduled_prompt_skill_source_matches_repo_workspace_skill(self) -> None:
        repo_skill = REPO_ROOT / ".agents" / "skills" / "feishu-scheduled-prompts"

        self.assertTrue(
            _skill_tree_matches_source(
                repo_skill,
                _managed_skill_source_dir("feishu-scheduled-prompts"),
            )
        )

    def test_pyproject_includes_scheduled_prompt_skill_payload(self) -> None:
        pyproject_path = REPO_ROOT / "pyproject.toml"
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

        package_data = data["tool"]["setuptools"]["package-data"]
        self.assertEqual(
            package_data["bot.managed_skills.feishu_scheduled_prompts"],
            [
                "skill/SKILL.md",
                "skill/agents/openai.yaml",
                "skill/scripts/__init__.py",
                "skill/scripts/manage_scheduled_prompt.py",
            ],
        )

    def test_pyproject_console_scripts_match_managed_public_commands(self) -> None:
        pyproject_path = REPO_ROOT / "pyproject.toml"
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

        self.assertEqual(
            data["project"]["scripts"],
            {
                spec.name: spec.console_script_target
                for spec in PUBLIC_COMMAND_SPECS
            },
        )

    def test_handle_skill_uninstall_removes_managed_skill_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = pathlib.Path(tmpdir) / "workspace"
            workspace.mkdir(parents=True, exist_ok=True)
            with patch.object(pathlib.Path, "cwd", return_value=workspace):
                _handle_skill_install()
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    result = _handle_skill_uninstall()

            self.assertEqual(result, 0)
            image_target = workspace / ".agents" / "skills" / "feishu-send-image"
            schedule_target = workspace / ".agents" / "skills" / "feishu-scheduled-prompts"
            self.assertFalse(image_target.exists())
            self.assertFalse(schedule_target.exists())
            rendered = stdout.getvalue()
            self.assertIn("已卸载 skill: feishu-send-image", rendered)
            self.assertIn("已卸载 skill: feishu-scheduled-prompts", rendered)

    def test_main_skill_subcommand_rejects_top_level_instance(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exc:
                main(["--instance", "corp-a", "skill", "install"])

        self.assertEqual(exc.exception.code, 2)
        self.assertIn("`focusctl skill ...` 不接受顶层 `--instance`", stderr.getvalue())

    def test_main_rejects_multiple_instances_for_run(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exc:
                main(["--instance", "default", "--instance", "corp-a", "run"])

        self.assertEqual(exc.exception.code, 2)
        self.assertIn("`run` 当前只支持单个实例", stderr.getvalue())

    def test_main_rejects_top_level_instance_for_instance_subcommands(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exc:
                main(["--instance", "default", "instance", "list"])

        self.assertEqual(exc.exception.code, 2)
        self.assertIn("`focusctl instance ...` 不接受顶层 `--instance`", stderr.getvalue())

    def test_named_instance_commands_do_not_implicitly_create_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "focus.env"
            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                with self.assertRaisesRegex(ValueError, "instance create corp-a"):
                    _handle_service_action("corp-a", "start")
                with self.assertRaisesRegex(ValueError, "instance create corp-a"):
                    _handle_config("corp-a", "system", open_editor=False)

            self.assertFalse((config_root / "instances" / "corp-a").exists())
            self.assertFalse((data_root / "instances" / "corp-a").exists())

    def test_config_env_does_not_require_named_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "focus.env"
            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    result = _handle_config("corp-a", "env", open_editor=False)

            self.assertEqual(result, 0)
            self.assertEqual(stdout.getvalue().strip(), str(env_file))
            self.assertTrue(env_file.exists())
            self.assertFalse((config_root / "instances" / "corp-a").exists())
            self.assertFalse((data_root / "instances" / "corp-a").exists())
