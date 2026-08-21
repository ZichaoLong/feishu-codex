#!/usr/bin/env python3
"""Reject imports that reverse Focus's current dependency direction."""

from __future__ import annotations

import pathlib
import sys
from dataclasses import dataclass

if __package__:
    from scripts import check_import_cycles
else:
    import check_import_cycles


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PACKAGE_ROOT = _REPO_ROOT / "bot"
_PACKAGE_NAME = "bot"


@dataclass(frozen=True, slots=True)
class ModuleSelector:
    packages: tuple[str, ...] = ()
    name_prefixes: tuple[str, ...] = ()
    exact_modules: tuple[str, ...] = ()

    def matches(self, module: str) -> bool:
        return bool(
            module in self.exact_modules
            or any(
                module == package or module.startswith(f"{package}.")
                for package in self.packages
            )
            or any(module.startswith(prefix) for prefix in self.name_prefixes)
        )


@dataclass(frozen=True, slots=True)
class DependencyDirectionRule:
    name: str
    source: ModuleSelector
    forbidden: ModuleSelector
    reason: str


@dataclass(frozen=True, slots=True)
class DependencyDirectionViolation:
    rule: str
    source: str
    dependency: str
    reason: str

    def render(self) -> str:
        return f"{self.source} -> {self.dependency} [{self.rule}]: {self.reason}"


@dataclass(frozen=True, slots=True)
class DependencyDirectionReport:
    module_count: int
    edge_count: int
    violations: tuple[DependencyDirectionViolation, ...]


_COMPOSITION = ModuleSelector(
    exact_modules=(
        "bot.__main__",
        "bot.codex_handler",
        "bot.focus_runtime",
        "bot.focus_runtime.runtime",
        "bot.handler",
        "bot.standalone",
    )
)
_WEB_DOMAIN = ModuleSelector(packages=("bot.web_runtime",))
_FEISHU_DOMAIN = ModuleSelector(name_prefixes=("bot.feishu_",))
_FCODEX_DOMAIN = ModuleSelector(packages=("bot.fcodex",))
_ADMIN_SURFACES = ModuleSelector(
    packages=("bot.manage_cli",),
    exact_modules=(
        "bot.focusctl",
        "bot.runtime_admin.cli",
        "bot.runtime_admin.cli_inputs",
        "bot.runtime_admin.controller",
    )
)
_ALL_SURFACES = ModuleSelector(
    packages=(
        *_ADMIN_SURFACES.packages,
        *_FCODEX_DOMAIN.packages,
        *_WEB_DOMAIN.packages,
    ),
    name_prefixes=_FEISHU_DOMAIN.name_prefixes,
    exact_modules=_ADMIN_SURFACES.exact_modules,
)
_CORE_FORBIDDEN = ModuleSelector(
    packages=_ALL_SURFACES.packages,
    name_prefixes=_ALL_SURFACES.name_prefixes,
    exact_modules=tuple(
        sorted(set(_ALL_SURFACES.exact_modules + _COMPOSITION.exact_modules))
    ),
)
_MANAGE_CLI_ENTRYPOINT_MODULES = (
    "bot.manage_cli.__main__",
    "bot.manage_cli.entrypoint",
)
_MANAGE_CLI_COMMAND_MODULES = (
    "bot.manage_cli.install_surface",
    "bot.manage_cli.instance_commands",
    "bot.manage_cli.service_commands",
)
_MANAGE_CLI_WORKSPACE_LIFECYCLE = "bot.managed_skills.workspace_lifecycle"
_BACKEND_RESET_CONTRACT = "bot.backend_reset.contract"
_BACKEND_RESET_SURFACES = (
    "bot.backend_reset.cli",
    "bot.backend_reset.presenter",
)
_BACKEND_RESET_EFFECT_OWNERS = (
    "bot.backend_reset.coordinator",
    "bot.backend_reset.interaction_coordinator",
    "bot.backend_reset.service",
)


DEFAULT_RULES = (
    DependencyDirectionRule(
        name="stores-do-not-depend-on-protocol-or-surfaces",
        source=ModuleSelector(packages=("bot.stores",)),
        forbidden=ModuleSelector(
            packages=(
                "bot.adapters",
                "bot.codex_protocol",
                *_ADMIN_SURFACES.packages,
                *_FCODEX_DOMAIN.packages,
                *_WEB_DOMAIN.packages,
            ),
            exact_modules=tuple(
                sorted(
                    set(
                        tuple(
                            module
                            for module in _COMPOSITION.exact_modules
                            if module != "bot.focus_runtime"
                        )
                        + _ADMIN_SURFACES.exact_modules
                        + (
                            "bot.feishu_bot",
                            "bot.feishu_ingress_controller",
                            "bot.feishu_outbound",
                            "bot.feishu_ws_proxy",
                        )
                    )
                )
            ),
        ),
        reason="durable/process state owners cannot depend upward on protocol, presentation, or composition",
    ),
    DependencyDirectionRule(
        name="protocol-does-not-depend-on-adapters-or-surfaces",
        source=ModuleSelector(packages=("bot.codex_protocol",)),
        forbidden=ModuleSelector(
            packages=("bot.adapters", *_CORE_FORBIDDEN.packages),
            name_prefixes=_CORE_FORBIDDEN.name_prefixes,
            exact_modules=_CORE_FORBIDDEN.exact_modules,
        ),
        reason="the app-server transport boundary cannot import application surfaces or its adapter consumer",
    ),
    DependencyDirectionRule(
        name="adapters-do-not-depend-on-surfaces",
        source=ModuleSelector(packages=("bot.adapters",)),
        forbidden=_CORE_FORBIDDEN,
        reason="Codex adapters translate protocol facts and cannot depend on presentation or composition",
    ),
    DependencyDirectionRule(
        name="web-does-not-depend-on-other-surfaces",
        source=_WEB_DOMAIN,
        forbidden=ModuleSelector(
            packages=(*_ADMIN_SURFACES.packages, *_FCODEX_DOMAIN.packages),
            name_prefixes=_FEISHU_DOMAIN.name_prefixes,
            exact_modules=_ADMIN_SURFACES.exact_modules,
        ),
        reason="the Web surface may coordinate shared owners but cannot import another presentation surface",
    ),
    DependencyDirectionRule(
        name="feishu-does-not-depend-on-other-surfaces",
        source=_FEISHU_DOMAIN,
        forbidden=ModuleSelector(
            packages=(
                *_ADMIN_SURFACES.packages,
                *_FCODEX_DOMAIN.packages,
                *_WEB_DOMAIN.packages,
            ),
            exact_modules=_ADMIN_SURFACES.exact_modules,
        ),
        reason="the Feishu surface may coordinate shared owners but cannot import another presentation surface",
    ),
    DependencyDirectionRule(
        name="fcodex-does-not-depend-on-other-surfaces",
        source=_FCODEX_DOMAIN,
        forbidden=ModuleSelector(
            packages=(*_ADMIN_SURFACES.packages, *_WEB_DOMAIN.packages),
            name_prefixes=_FEISHU_DOMAIN.name_prefixes,
            exact_modules=_ADMIN_SURFACES.exact_modules,
        ),
        reason="the fcodex surface may coordinate shared owners but cannot import another presentation surface",
    ),
    DependencyDirectionRule(
        name="runtime-admin-application-is-surface-neutral",
        source=ModuleSelector(
            exact_modules=(
                "bot.runtime_admin.binding_application",
                "bot.runtime_admin.control_router",
                "bot.runtime_admin.offline_lifecycle",
                "bot.runtime_admin.binding_clear",
            )
        ),
        forbidden=_ALL_SURFACES,
        reason="surface-neutral Runtime Admin transactions cannot import Feishu, Web, fcodex, or CLI presentation",
    ),
    DependencyDirectionRule(
        name="manage-cli-errors-do-not-depend-upward",
        source=ModuleSelector(exact_modules=("bot.manage_cli.errors",)),
        forbidden=ModuleSelector(
            exact_modules=(
                *_MANAGE_CLI_ENTRYPOINT_MODULES,
                "bot.manage_cli.provisioning",
                *_MANAGE_CLI_COMMAND_MODULES,
                _MANAGE_CLI_WORKSPACE_LIFECYCLE,
            )
        ),
        reason="the shared Manage CLI error type cannot depend on command or presentation owners",
    ),
    DependencyDirectionRule(
        name="manage-cli-provisioning-does-not-depend-upward",
        source=ModuleSelector(exact_modules=("bot.manage_cli.provisioning",)),
        forbidden=ModuleSelector(
            exact_modules=(
                *_MANAGE_CLI_ENTRYPOINT_MODULES,
                *_MANAGE_CLI_COMMAND_MODULES,
                _MANAGE_CLI_WORKSPACE_LIFECYCLE,
            )
        ),
        reason="shared Manage CLI provisioning primitives cannot import command or presentation owners",
    ),
    DependencyDirectionRule(
        name="manage-cli-service-commands-do-not-depend-sideways-or-upward",
        source=ModuleSelector(exact_modules=("bot.manage_cli.service_commands",)),
        forbidden=ModuleSelector(
            exact_modules=(
                *_MANAGE_CLI_ENTRYPOINT_MODULES,
                "bot.manage_cli.install_surface",
                "bot.manage_cli.instance_commands",
                _MANAGE_CLI_WORKSPACE_LIFECYCLE,
            )
        ),
        reason="service commands may depend on provisioning but not sibling commands or presentation",
    ),
    DependencyDirectionRule(
        name="manage-cli-install-surface-does-not-depend-sideways-or-upward",
        source=ModuleSelector(exact_modules=("bot.manage_cli.install_surface",)),
        forbidden=ModuleSelector(
            exact_modules=(
                *_MANAGE_CLI_ENTRYPOINT_MODULES,
                "bot.manage_cli.instance_commands",
                "bot.manage_cli.service_commands",
                _MANAGE_CLI_WORKSPACE_LIFECYCLE,
            )
        ),
        reason="the install surface may depend on provisioning but not sibling commands or presentation",
    ),
    DependencyDirectionRule(
        name="manage-cli-instance-commands-follow-service-observation",
        source=ModuleSelector(exact_modules=("bot.manage_cli.instance_commands",)),
        forbidden=ModuleSelector(
            exact_modules=(
                *_MANAGE_CLI_ENTRYPOINT_MODULES,
                "bot.manage_cli.install_surface",
                _MANAGE_CLI_WORKSPACE_LIFECYCLE,
            )
        ),
        reason="instance commands may read service projections but cannot depend on install or presentation owners",
    ),
    DependencyDirectionRule(
        name="managed-skill-workspace-lifecycle-is-manage-cli-independent",
        source=ModuleSelector(exact_modules=(_MANAGE_CLI_WORKSPACE_LIFECYCLE,)),
        forbidden=ModuleSelector(packages=("bot.manage_cli",)),
        reason="workspace skill lifecycle is a standalone capability owner below Manage CLI routing",
    ),
    DependencyDirectionRule(
        name="manage-cli-module-entrypoint-only-loads-router",
        source=ModuleSelector(exact_modules=("bot.manage_cli.__main__",)),
        forbidden=ModuleSelector(
            exact_modules=(
                "bot.manage_cli.errors",
                "bot.manage_cli.provisioning",
                *_MANAGE_CLI_COMMAND_MODULES,
                _MANAGE_CLI_WORKSPACE_LIFECYCLE,
            )
        ),
        reason="python -m bot.manage_cli must delegate only to the real entrypoint",
    ),
    DependencyDirectionRule(
        name="focus-runtime-capabilities-do-not-depend-on-composition-root",
        source=ModuleSelector(packages=("bot.focus_runtime",)),
        forbidden=ModuleSelector(exact_modules=("bot.focus_runtime.runtime",)),
        reason=(
            "the empty package root and extracted capability owners cannot "
            "reverse-depend on the FocusRuntime composition root"
        ),
    ),
    DependencyDirectionRule(
        name="service-runtime-authority-does-not-own-lifecycle-or-presentation",
        source=ModuleSelector(
            exact_modules=("bot.focus_runtime.service_authority",)
        ),
        forbidden=ModuleSelector(
            packages=_CORE_FORBIDDEN.packages,
            name_prefixes=_CORE_FORBIDDEN.name_prefixes,
            exact_modules=tuple(
                sorted(
                    set(
                        _CORE_FORBIDDEN.exact_modules
                        + _BACKEND_RESET_SURFACES
                        + ("bot.service_runtime_lifecycle",)
                    )
                )
            ),
        ),
        reason=(
            "machine-visible runtime coordination cannot own lifecycle phase "
            "or depend upward on composition and presentation"
        ),
    ),
    DependencyDirectionRule(
        name="authoritative-thread-targets-do-not-depend-on-presentation",
        source=ModuleSelector(exact_modules=("bot.focus_runtime.thread_targets",)),
        forbidden=ModuleSelector(
            packages=_CORE_FORBIDDEN.packages,
            name_prefixes=_CORE_FORBIDDEN.name_prefixes,
            exact_modules=tuple(
                sorted(
                    set(
                        _CORE_FORBIDDEN.exact_modules
                        + _BACKEND_RESET_SURFACES
                        + ("bot.service_runtime_lifecycle",)
                    )
                )
            ),
        ),
        reason=(
            "authoritative Codex thread targeting stays below lifecycle, "
            "composition, and presentation surfaces"
        ),
    ),
    DependencyDirectionRule(
        name="binding-runtime-coordination-stays-below-presentation",
        source=ModuleSelector(
            exact_modules=("bot.focus_runtime.binding_coordinator",)
        ),
        forbidden=ModuleSelector(
            packages=(
                *_ADMIN_SURFACES.packages,
                *_FCODEX_DOMAIN.packages,
                *_WEB_DOMAIN.packages,
            ),
            exact_modules=tuple(
                sorted(
                    set(
                        tuple(
                            module
                            for module in _COMPOSITION.exact_modules
                            if module != "bot.focus_runtime"
                        )
                        + _ADMIN_SURFACES.exact_modules
                        + _BACKEND_RESET_SURFACES
                        + (
                            "bot.feishu_bot",
                            "bot.feishu_ingress_controller",
                            "bot.feishu_outbound",
                            "bot.feishu_ws_proxy",
                            "bot.service_runtime_lifecycle",
                        )
                    )
                )
            ),
        ),
        reason=(
            "binding transactions may use Feishu capability owners but cannot "
            "depend upward on lifecycle, composition, or presentation"
        ),
    ),
    DependencyDirectionRule(
        name="feishu-platform-owns-no-surface-or-persistence",
        source=ModuleSelector(
            exact_modules=("bot.focus_runtime.feishu_platform",)
        ),
        forbidden=ModuleSelector(
            packages=(
                "bot.stores",
                *_ADMIN_SURFACES.packages,
                *_FCODEX_DOMAIN.packages,
                *_WEB_DOMAIN.packages,
            ),
            exact_modules=tuple(
                sorted(
                    set(
                        tuple(
                            module
                            for module in _COMPOSITION.exact_modules
                            if module != "bot.focus_runtime"
                        )
                        + _ADMIN_SURFACES.exact_modules
                        + _BACKEND_RESET_SURFACES
                        + _BACKEND_RESET_EFFECT_OWNERS
                        + (
                            "bot.feishu_bot",
                            "bot.focus_runtime.feishu_surface",
                            "bot.focus_runtime.terminal_results",
                            "bot.inbound_surface_controller",
                            "bot.service_runtime_lifecycle",
                        )
                    )
                )
            ),
        ),
        reason=(
            "the attached Feishu bot fact and platform routing stay below "
            "surface, lifecycle, composition, and persistence owners"
        ),
    ),
    DependencyDirectionRule(
        name="terminal-results-stay-below-surfaces",
        source=ModuleSelector(
            exact_modules=("bot.focus_runtime.terminal_results",)
        ),
        forbidden=ModuleSelector(
            packages=(
                *_ADMIN_SURFACES.packages,
                *_FCODEX_DOMAIN.packages,
                *_WEB_DOMAIN.packages,
            ),
            exact_modules=tuple(
                sorted(
                    set(
                        tuple(
                            module
                            for module in _COMPOSITION.exact_modules
                            if module != "bot.focus_runtime"
                        )
                        + _ADMIN_SURFACES.exact_modules
                        + _BACKEND_RESET_SURFACES
                        + _BACKEND_RESET_EFFECT_OWNERS
                        + (
                            "bot.execution_output_controller",
                            "bot.feishu_bot",
                            "bot.focus_runtime.feishu_surface",
                            "bot.inbound_surface_controller",
                            "bot.service_runtime_lifecycle",
                        )
                    )
                )
            ),
        ),
        reason=(
            "terminal-result projection may consume typed ports and the "
            "platform capability but cannot depend upward on concrete surfaces"
        ),
    ),
    DependencyDirectionRule(
        name="feishu-runtime-surface-does-not-import-peer-surfaces",
        source=ModuleSelector(
            exact_modules=("bot.focus_runtime.feishu_surface",)
        ),
        forbidden=ModuleSelector(
            packages=(
                *_ADMIN_SURFACES.packages,
                *_FCODEX_DOMAIN.packages,
                *_WEB_DOMAIN.packages,
            ),
            exact_modules=tuple(
                sorted(
                    set(
                        tuple(
                            module
                            for module in _COMPOSITION.exact_modules
                            if module != "bot.focus_runtime"
                        )
                        + _BACKEND_RESET_SURFACES
                        + _BACKEND_RESET_EFFECT_OWNERS
                        + (
                            "bot.feishu_bot",
                            "bot.service_runtime_lifecycle",
                        )
                    )
                )
            ),
        ),
        reason=(
            "the Feishu runtime surface may compose Feishu and shared owners "
            "but cannot import peer surfaces, lifecycle, or the composition root"
        ),
    ),
    DependencyDirectionRule(
        name="backend-reset-package-root-has-no-behavior",
        source=ModuleSelector(exact_modules=("bot.backend_reset",)),
        forbidden=ModuleSelector(
            exact_modules=(
                _BACKEND_RESET_CONTRACT,
                *_BACKEND_RESET_SURFACES,
                *_BACKEND_RESET_EFFECT_OWNERS,
            )
        ),
        reason="the backend-reset package root cannot become a facade or behavior owner",
    ),
    DependencyDirectionRule(
        name="backend-reset-contract-is-foundational",
        source=ModuleSelector(exact_modules=(_BACKEND_RESET_CONTRACT,)),
        forbidden=ModuleSelector(
            exact_modules=(*_BACKEND_RESET_SURFACES, *_BACKEND_RESET_EFFECT_OWNERS)
        ),
        reason="backend-reset vocabulary cannot depend on surfaces or effect owners",
    ),
    DependencyDirectionRule(
        name="backend-reset-interaction-inventory-is-foundational",
        source=ModuleSelector(
            exact_modules=("bot.backend_reset.interaction_coordinator",)
        ),
        forbidden=ModuleSelector(
            exact_modules=(
                *_BACKEND_RESET_SURFACES,
                "bot.backend_reset.coordinator",
                "bot.backend_reset.service",
            )
        ),
        reason="the pending-request inventory cannot depend on reset surfaces or transactions",
    ),
    DependencyDirectionRule(
        name="backend-reset-coordinator-depends-only-on-contract",
        source=ModuleSelector(exact_modules=("bot.backend_reset.coordinator",)),
        forbidden=ModuleSelector(
            exact_modules=(
                *_BACKEND_RESET_SURFACES,
                "bot.backend_reset.interaction_coordinator",
                "bot.backend_reset.service",
            )
        ),
        reason="the epoch coordinator may depend on reset vocabulary but not sibling effects or surfaces",
    ),
    DependencyDirectionRule(
        name="backend-reset-service-follows-foundational-owners",
        source=ModuleSelector(exact_modules=("bot.backend_reset.service",)),
        forbidden=ModuleSelector(
            exact_modules=(
                *_BACKEND_RESET_SURFACES,
                "bot.backend_reset.coordinator",
            )
        ),
        reason="the product transaction may consume contract and interaction inventory but not surfaces or the concrete epoch coordinator",
    ),
    DependencyDirectionRule(
        name="backend-reset-surfaces-depend-only-on-contract",
        source=ModuleSelector(exact_modules=_BACKEND_RESET_SURFACES),
        forbidden=ModuleSelector(
            exact_modules=(*_BACKEND_RESET_SURFACES, *_BACKEND_RESET_EFFECT_OWNERS)
        ),
        reason="backend-reset CLI and presenter projections cannot own or import reset effects",
    ),
    DependencyDirectionRule(
        name="interaction-approval-cards-stay-presentation-only",
        source=ModuleSelector(
            exact_modules=("bot.interaction_approval_cards",)
        ),
        forbidden=ModuleSelector(
            packages=(
                "bot.adapters",
                "bot.codex_protocol",
                "bot.focus_runtime",
                "bot.stores",
            ),
            exact_modules=(
                "bot.cards",
                "bot.interaction_request_controller",
                "bot.runtime_card_publisher",
            ),
        ),
        reason="canonical approval card dictionaries cannot depend on the mixed card catalog, runtime authority, protocol, stores, or effects",
    ),
    DependencyDirectionRule(
        name="approval-settings-cards-stay-presentation-only",
        source=ModuleSelector(exact_modules=("bot.approval_settings_cards",)),
        forbidden=ModuleSelector(
            packages=(
                "bot.adapters",
                "bot.codex_protocol",
                "bot.focus_runtime",
                "bot.stores",
            ),
            exact_modules=(
                "bot.binding_runtime_contract",
                "bot.cards",
                "bot.codex_settings_domain",
                "bot.config",
                "bot.runtime_card_publisher",
                "bot.system_config",
            ),
        ),
        reason="approval and permissions setting card dictionaries cannot depend on runtime setting authority, the mixed card catalog, protocol, stores, or effects",
    ),
)


def check_graph(
    graph: dict[str, frozenset[str]],
    *,
    rules: tuple[DependencyDirectionRule, ...] = DEFAULT_RULES,
) -> DependencyDirectionReport:
    violations = tuple(
        sorted(
            (
                DependencyDirectionViolation(
                    rule=rule.name,
                    source=source,
                    dependency=dependency,
                    reason=rule.reason,
                )
                for rule in rules
                for source, dependencies in graph.items()
                if rule.source.matches(source)
                for dependency in dependencies
                if rule.forbidden.matches(dependency)
            ),
            key=lambda item: (item.rule, item.source, item.dependency),
        )
    )
    return DependencyDirectionReport(
        module_count=len(graph),
        edge_count=sum(len(dependencies) for dependencies in graph.values()),
        violations=violations,
    )


def check_package(
    package_root: pathlib.Path,
    *,
    package_name: str,
    rules: tuple[DependencyDirectionRule, ...] = DEFAULT_RULES,
) -> DependencyDirectionReport:
    graph = check_import_cycles.build_import_graph(
        package_root,
        package_name=package_name,
    )
    return check_graph(graph, rules=rules)


def check() -> DependencyDirectionReport:
    return check_package(_PACKAGE_ROOT, package_name=_PACKAGE_NAME)


def main() -> int:
    try:
        report = check()
    except check_import_cycles.ImportGraphError as exc:
        print(
            f"Python dependency-direction guard could not inspect the package: {exc}",
            file=sys.stderr,
        )
        return 2
    if report.violations:
        print("Python dependency-direction guard found forbidden imports:", file=sys.stderr)
        for violation in report.violations:
            print(f"- {violation.render()}", file=sys.stderr)
        return 1
    print(
        "Python dependency direction is valid "
        f"({report.module_count} modules, {report.edge_count} internal edges, "
        f"{len(DEFAULT_RULES)} rules)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
