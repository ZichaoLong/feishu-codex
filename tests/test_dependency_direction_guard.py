from __future__ import annotations

import unittest

from scripts import check_dependency_direction


class DependencyDirectionGuardTests(unittest.TestCase):
    def test_selector_distinguishes_packages_name_prefixes_and_exact_modules(
        self,
    ) -> None:
        selector = check_dependency_direction.ModuleSelector(
            packages=("sample.core",),
            name_prefixes=("sample.surface_",),
            exact_modules=("sample.entry",),
        )

        self.assertTrue(selector.matches("sample.core"))
        self.assertTrue(selector.matches("sample.core.child"))
        self.assertTrue(selector.matches("sample.surface_web"))
        self.assertTrue(selector.matches("sample.entry"))
        self.assertFalse(selector.matches("sample.coreish"))
        self.assertFalse(selector.matches("sample.surface"))

        for fcodex_selector in (
            check_dependency_direction._FCODEX_DOMAIN,
            check_dependency_direction._ALL_SURFACES,
            check_dependency_direction._CORE_FORBIDDEN,
        ):
            self.assertTrue(fcodex_selector.matches("bot.fcodex"))
            self.assertTrue(fcodex_selector.matches("bot.fcodex.proxy"))
            self.assertFalse(fcodex_selector.matches("bot.fcodexish"))

        for web_selector in (
            check_dependency_direction._WEB_DOMAIN,
            check_dependency_direction._ALL_SURFACES,
            check_dependency_direction._CORE_FORBIDDEN,
        ):
            self.assertTrue(web_selector.matches("bot.web_runtime"))
            self.assertTrue(web_selector.matches("bot.web_runtime.gateway"))
            self.assertFalse(web_selector.matches("bot.web_runtimeish"))
            self.assertFalse(web_selector.matches("bot.web_runtime_controller"))
            self.assertFalse(web_selector.matches("bot.web_assets"))

        for admin_selector in (
            check_dependency_direction._ADMIN_SURFACES,
            check_dependency_direction._ALL_SURFACES,
            check_dependency_direction._CORE_FORBIDDEN,
        ):
            self.assertTrue(admin_selector.matches("bot.runtime_admin.cli_inputs"))
            self.assertFalse(admin_selector.matches("bot.runtime_admin.cli_input"))

    def test_forbidden_edge_reports_exact_rule_and_modules(self) -> None:
        rule = check_dependency_direction.DependencyDirectionRule(
            name="core-does-not-import-surface",
            source=check_dependency_direction.ModuleSelector(
                packages=("sample.core",)
            ),
            forbidden=check_dependency_direction.ModuleSelector(
                packages=("sample.surface",)
            ),
            reason="dependency must point toward core",
        )

        report = check_dependency_direction.check_graph(
            {
                "sample": frozenset(),
                "sample.core": frozenset({"sample", "sample.surface"}),
                "sample.surface": frozenset({"sample", "sample.core"}),
            },
            rules=(rule,),
        )

        self.assertEqual(
            report.violations,
            (
                check_dependency_direction.DependencyDirectionViolation(
                    rule="core-does-not-import-surface",
                    source="sample.core",
                    dependency="sample.surface",
                    reason="dependency must point toward core",
                ),
            ),
        )

    def test_allowed_forward_and_same_domain_edges_pass(self) -> None:
        rule = check_dependency_direction.DependencyDirectionRule(
            name="core-does-not-import-surface",
            source=check_dependency_direction.ModuleSelector(
                packages=("sample.core",)
            ),
            forbidden=check_dependency_direction.ModuleSelector(
                packages=("sample.surface",)
            ),
            reason="dependency must point toward core",
        )

        report = check_dependency_direction.check_graph(
            {
                "sample": frozenset(),
                "sample.core": frozenset({"sample", "sample.core.contract"}),
                "sample.core.contract": frozenset({"sample"}),
                "sample.surface": frozenset({"sample", "sample.core"}),
            },
            rules=(rule,),
        )

        self.assertEqual(report.violations, ())

    def test_manage_cli_package_rules_reject_upward_and_sideways_edges(self) -> None:
        report = check_dependency_direction.check_graph(
            {
                "bot.manage_cli.errors": frozenset(),
                "bot.manage_cli.provisioning": frozenset(
                    {"bot.manage_cli.errors", "bot.manage_cli.entrypoint"}
                ),
                "bot.manage_cli.service_commands": frozenset(
                    {
                        "bot.manage_cli.provisioning",
                        "bot.manage_cli.instance_commands",
                    }
                ),
                "bot.managed_skills.workspace_lifecycle": frozenset(
                    {"bot.manage_cli.provisioning"}
                ),
            }
        )

        self.assertEqual(
            tuple(
                (item.rule, item.source, item.dependency)
                for item in report.violations
            ),
            (
                (
                    "manage-cli-provisioning-does-not-depend-upward",
                    "bot.manage_cli.provisioning",
                    "bot.manage_cli.entrypoint",
                ),
                (
                    "manage-cli-service-commands-do-not-depend-sideways-or-upward",
                    "bot.manage_cli.service_commands",
                    "bot.manage_cli.instance_commands",
                ),
                (
                    "managed-skill-workspace-lifecycle-is-manage-cli-independent",
                    "bot.managed_skills.workspace_lifecycle",
                    "bot.manage_cli.provisioning",
                ),
            ),
        )

    def test_backend_reset_package_rules_reject_facades_and_reverse_edges(self) -> None:
        report = check_dependency_direction.check_graph(
            {
                "bot.backend_reset": frozenset({"bot.backend_reset.contract"}),
                "bot.backend_reset.contract": frozenset(
                    {"bot.backend_reset.service"}
                ),
                "bot.backend_reset.coordinator": frozenset(
                    {"bot.backend_reset.contract"}
                ),
                "bot.backend_reset.cli": frozenset({"bot.backend_reset.service"}),
                "bot.backend_reset.service": frozenset(
                    {"bot.backend_reset.coordinator"}
                ),
            }
        )

        self.assertEqual(
            {
                (item.rule, item.source, item.dependency)
                for item in report.violations
            },
            {
                (
                    "backend-reset-package-root-has-no-behavior",
                    "bot.backend_reset",
                    "bot.backend_reset.contract",
                ),
                (
                    "backend-reset-contract-is-foundational",
                    "bot.backend_reset.contract",
                    "bot.backend_reset.service",
                ),
                (
                    "backend-reset-surfaces-depend-only-on-contract",
                    "bot.backend_reset.cli",
                    "bot.backend_reset.service",
                ),
                (
                    "backend-reset-service-follows-foundational-owners",
                    "bot.backend_reset.service",
                    "bot.backend_reset.coordinator",
                ),
            },
        )

    def test_focus_runtime_package_rejects_root_reexports_and_reverse_edges(self) -> None:
        report = check_dependency_direction.check_graph(
            {
                "bot.focus_runtime": frozenset({"bot.focus_runtime.runtime"}),
                "bot.focus_runtime.runtime": frozenset({"bot.shared_owner"}),
                "bot.focus_runtime.capability": frozenset(
                    {"bot.focus_runtime.runtime"}
                ),
            }
        )

        self.assertEqual(
            {
                (item.rule, item.source, item.dependency)
                for item in report.violations
            },
            {
                (
                    "focus-runtime-capabilities-do-not-depend-on-composition-root",
                    "bot.focus_runtime",
                    "bot.focus_runtime.runtime",
                ),
                (
                    "focus-runtime-capabilities-do-not-depend-on-composition-root",
                    "bot.focus_runtime.capability",
                    "bot.focus_runtime.runtime",
                ),
            },
        )

    def test_service_runtime_authority_rejects_lifecycle_and_surfaces(self) -> None:
        report = check_dependency_direction.check_graph(
            {
                "bot.focus_runtime.service_authority": frozenset(
                    {
                        "bot.backend_reset.presenter",
                        "bot.codex_handler",
                        "bot.fcodex.operation_service",
                        "bot.feishu_bot",
                        "bot.focus_runtime.runtime",
                        "bot.manage_cli.entrypoint",
                        "bot.runtime_admin.cli",
                        "bot.runtime_admin.cli_inputs",
                        "bot.service_runtime_lifecycle",
                        "bot.stores.instance_registry_store",
                        "bot.thread_runtime_coordination",
                        "bot.web_runtime.gateway",
                    }
                ),
            }
        )

        self.assertEqual(
            {
                (item.source, item.dependency)
                for item in report.violations
                if item.rule
                == "service-runtime-authority-does-not-own-lifecycle-or-presentation"
            },
            {
                ("bot.focus_runtime.service_authority", dependency)
                for dependency in {
                    "bot.backend_reset.presenter",
                    "bot.codex_handler",
                    "bot.fcodex.operation_service",
                    "bot.feishu_bot",
                    "bot.focus_runtime.runtime",
                    "bot.manage_cli.entrypoint",
                    "bot.runtime_admin.cli",
                    "bot.runtime_admin.cli_inputs",
                    "bot.service_runtime_lifecycle",
                    "bot.web_runtime.gateway",
                }
            },
        )

    def test_authoritative_thread_targets_reject_presentation_dependencies(self) -> None:
        report = check_dependency_direction.check_graph(
            {
                "bot.focus_runtime.thread_targets": frozenset(
                    {
                        "bot.adapters.codex_app_server",
                        "bot.binding_runtime_manager",
                        "bot.fcodex.operation_service",
                        "bot.feishu_bot",
                        "bot.focus_runtime.runtime",
                        "bot.runtime_admin.controller",
                        "bot.service_runtime_lifecycle",
                        "bot.thread_runtime_authority",
                        "bot.web_runtime.gateway",
                    }
                ),
            }
        )

        self.assertEqual(
            {
                (item.source, item.dependency)
                for item in report.violations
                if item.rule
                == "authoritative-thread-targets-do-not-depend-on-presentation"
            },
            {
                ("bot.focus_runtime.thread_targets", dependency)
                for dependency in {
                    "bot.fcodex.operation_service",
                    "bot.feishu_bot",
                    "bot.focus_runtime.runtime",
                    "bot.runtime_admin.controller",
                    "bot.service_runtime_lifecycle",
                    "bot.web_runtime.gateway",
                }
            },
        )

    def test_binding_runtime_coordinator_allows_capabilities_not_surfaces(self) -> None:
        report = check_dependency_direction.check_graph(
            {
                "bot.focus_runtime.binding_coordinator": frozenset(
                    {
                        "bot.backend_reset.presenter",
                        "bot.binding_runtime_manager",
                        "bot.fcodex.operation_service",
                        "bot.feishu_binding_transition",
                        "bot.feishu_bot",
                        "bot.feishu_execution_queue",
                        "bot.focus_runtime.runtime",
                        "bot.runtime_admin.binding_clear",
                        "bot.runtime_admin.controller",
                        "bot.service_runtime_lifecycle",
                        "bot.thread_runtime_authority",
                        "bot.web_runtime.controller",
                    }
                ),
            }
        )

        self.assertEqual(
            {
                (item.source, item.dependency)
                for item in report.violations
                if item.rule
                == "binding-runtime-coordination-stays-below-presentation"
            },
            {
                ("bot.focus_runtime.binding_coordinator", dependency)
                for dependency in {
                    "bot.backend_reset.presenter",
                    "bot.fcodex.operation_service",
                    "bot.feishu_bot",
                    "bot.focus_runtime.runtime",
                    "bot.runtime_admin.controller",
                    "bot.service_runtime_lifecycle",
                    "bot.web_runtime.controller",
                }
            },
        )

    def test_focus_runtime_feishu_owners_reject_reverse_dependencies(self) -> None:
        report = check_dependency_direction.check_graph(
            {
                "bot.focus_runtime.feishu_platform": frozenset(
                    {
                        "bot.focus_runtime.feishu_surface",
                        "bot.runtime_card_publisher",
                        "bot.stores.chat_binding_store",
                        "bot.web_runtime.gateway",
                    }
                ),
                "bot.focus_runtime.terminal_results": frozenset(
                    {
                        "bot.execution_output_controller",
                        "bot.focus_runtime.feishu_platform",
                        "bot.focus_runtime.feishu_surface",
                        "bot.stores.terminal_result_store",
                    }
                ),
                "bot.focus_runtime.feishu_surface": frozenset(
                    {
                        "bot.focus_runtime.feishu_platform",
                        "bot.focus_runtime.runtime",
                        "bot.focus_runtime.terminal_results",
                        "bot.runtime_admin.controller",
                        "bot.web_runtime.controller",
                    }
                ),
            }
        )

        by_rule = {
            rule: {
                (item.source, item.dependency)
                for item in report.violations
                if item.rule == rule
            }
            for rule in {
                "feishu-platform-owns-no-surface-or-persistence",
                "terminal-results-stay-below-surfaces",
                "feishu-runtime-surface-does-not-import-peer-surfaces",
            }
        }
        self.assertEqual(
            by_rule,
            {
                "feishu-platform-owns-no-surface-or-persistence": {
                    (
                        "bot.focus_runtime.feishu_platform",
                        "bot.focus_runtime.feishu_surface",
                    ),
                    (
                        "bot.focus_runtime.feishu_platform",
                        "bot.stores.chat_binding_store",
                    ),
                    (
                        "bot.focus_runtime.feishu_platform",
                        "bot.web_runtime.gateway",
                    ),
                },
                "terminal-results-stay-below-surfaces": {
                    (
                        "bot.focus_runtime.terminal_results",
                        "bot.execution_output_controller",
                    ),
                    (
                        "bot.focus_runtime.terminal_results",
                        "bot.focus_runtime.feishu_surface",
                    ),
                },
                "feishu-runtime-surface-does-not-import-peer-surfaces": {
                    (
                        "bot.focus_runtime.feishu_surface",
                        "bot.focus_runtime.runtime",
                    ),
                    (
                        "bot.focus_runtime.feishu_surface",
                        "bot.web_runtime.controller",
                    ),
                },
            },
        )

    def test_interaction_approval_cards_reject_upward_dependencies(self) -> None:
        report = check_dependency_direction.check_graph(
            {
                "bot.interaction_approval_cards": frozenset(
                    {
                        "bot.cards",
                        "bot.constants",
                        "bot.interaction_request_controller",
                        "bot.runtime_card_publisher",
                        "bot.stores.chat_binding_store",
                    }
                )
            }
        )

        self.assertEqual(
            {
                (item.source, item.dependency)
                for item in report.violations
                if item.rule
                == "interaction-approval-cards-stay-presentation-only"
            },
            {
                ("bot.interaction_approval_cards", "bot.cards"),
                (
                    "bot.interaction_approval_cards",
                    "bot.interaction_request_controller",
                ),
                (
                    "bot.interaction_approval_cards",
                    "bot.runtime_card_publisher",
                ),
                (
                    "bot.interaction_approval_cards",
                    "bot.stores.chat_binding_store",
                ),
            },
        )

    def test_approval_settings_cards_reject_upward_dependencies(self) -> None:
        report = check_dependency_direction.check_graph(
            {
                "bot.approval_settings_cards": frozenset(
                    {
                        "bot.adapters.base",
                        "bot.binding_runtime_contract",
                        "bot.cards",
                        "bot.codex_settings_domain",
                        "bot.config",
                        "bot.permissions_profile",
                        "bot.runtime_card_publisher",
                        "bot.stores.binding_store",
                    }
                )
            }
        )

        self.assertEqual(
            {
                (item.source, item.dependency)
                for item in report.violations
                if item.rule
                == "approval-settings-cards-stay-presentation-only"
            },
            {
                ("bot.approval_settings_cards", "bot.adapters.base"),
                (
                    "bot.approval_settings_cards",
                    "bot.binding_runtime_contract",
                ),
                ("bot.approval_settings_cards", "bot.cards"),
                ("bot.approval_settings_cards", "bot.codex_settings_domain"),
                ("bot.approval_settings_cards", "bot.config"),
                (
                    "bot.approval_settings_cards",
                    "bot.runtime_card_publisher",
                ),
                ("bot.approval_settings_cards", "bot.stores.binding_store"),
            },
        )

    def test_focus_package_obeys_declared_dependency_direction(self) -> None:
        report = check_dependency_direction.check()

        self.assertEqual(report.violations, ())
        self.assertGreaterEqual(len(check_dependency_direction.DEFAULT_RULES), 7)


if __name__ == "__main__":
    unittest.main()
