"""Tests for container tools (toolbox & podman integration)."""

import pytest
from shellgenius.engine.containers import (
    ContainerRuntime,
    ContainerState,
    SandboxLevel,
    SandboxProfile,
    SandboxExecutor,
    ToolboxTool,
    PodmanTool,
    SANDBOX_PROFILES,
    detect_runtimes,
)
from shellgenius.knowledge.codex_matrix import (
    MATRIX,
    Equivalence,
    summary,
    ToolMapping,
)
from shellgenius.agent import ShellGeniusAgent, AgentContext, Intent


# --- Sandbox Profiles ---

class TestSandboxProfiles:
    def test_all_levels_defined(self):
        for level in SandboxLevel:
            assert level in SANDBOX_PROFILES

    def test_none_profile_has_no_restrictions(self):
        p = SANDBOX_PROFILES[SandboxLevel.NONE]
        assert not p.read_only
        assert not p.cap_drop
        assert not p.no_new_privileges
        assert p.pids_limit == 0

    def test_locked_profile_is_maximally_restrictive(self):
        p = SANDBOX_PROFILES[SandboxLevel.LOCKED]
        assert p.read_only is True
        assert p.network == "none"
        assert "ALL" in p.cap_drop
        assert p.no_new_privileges is True
        assert p.pids_limit > 0
        assert p.memory != ""
        assert p.cpus > 0
        assert len(p.tmpfs) > 0

    def test_restricted_has_no_network(self):
        p = SANDBOX_PROFILES[SandboxLevel.RESTRICTED]
        assert p.network == "none"
        assert "ALL" in p.cap_drop

    def test_workspace_has_network(self):
        p = SANDBOX_PROFILES[SandboxLevel.WORKSPACE]
        assert p.network == "host"

    def test_profiles_are_ordered_by_restriction(self):
        """Verify that profiles get progressively more restrictive."""
        none = SANDBOX_PROFILES[SandboxLevel.NONE]
        workspace = SANDBOX_PROFILES[SandboxLevel.WORKSPACE]
        restricted = SANDBOX_PROFILES[SandboxLevel.RESTRICTED]
        locked = SANDBOX_PROFILES[SandboxLevel.LOCKED]
        # None < Workspace < Restricted < Locked (by restriction level)
        assert not none.no_new_privileges
        assert workspace.no_new_privileges
        assert restricted.pids_limit > 0
        assert locked.pids_limit < restricted.pids_limit
        assert locked.read_only and not restricted.read_only


class TestSandboxProfileFlags:
    def test_locked_to_flags(self):
        flags = SANDBOX_PROFILES[SandboxLevel.LOCKED].to_flags()
        assert "--network=none" in flags
        assert "--read-only" in flags
        assert "--cap-drop=ALL" in flags
        assert "--security-opt=no-new-privileges" in flags
        assert any("--pids-limit=" in f for f in flags)
        assert any("--memory=" in f for f in flags)
        assert any("--cpus=" in f for f in flags)
        assert any("--tmpfs=" in f for f in flags)

    def test_none_to_flags_is_empty(self):
        flags = SANDBOX_PROFILES[SandboxLevel.NONE].to_flags()
        assert flags == []

    def test_custom_profile_flags(self):
        p = SandboxProfile(
            level=SandboxLevel.RESTRICTED,
            network="none",
            read_only=True,
            cap_drop=("ALL",),
            cap_add=("NET_RAW",),
            user="1000:1000",
            workdir="/workspace",
            volumes=("./src:/workspace:ro",),
        )
        flags = p.to_flags()
        assert "--network=none" in flags
        assert "--read-only" in flags
        assert "--cap-drop=ALL" in flags
        assert "--cap-add=NET_RAW" in flags
        assert "--user=1000:1000" in flags
        assert "--workdir=/workspace" in flags
        assert "-v=./src:/workspace:ro" in flags


# --- Container State ---

class TestContainerState:
    def test_all_states_have_values(self):
        for state in ContainerState:
            assert state.value  # not empty string

    def test_state_enum_covers_podman_states(self):
        podman_states = {"created", "running", "paused", "exited", "stopped"}
        enum_values = {s.value for s in ContainerState}
        for ps in podman_states:
            assert ps in enum_values


# --- Sandbox Executor ---

class TestSandboxExecutor:
    def test_describe_all_levels(self):
        executor = SandboxExecutor(runtimes={})
        for level in SandboxLevel:
            desc = executor.describe_sandbox(level)
            assert len(desc) > 20  # non-trivial description
            assert isinstance(desc, str)

    def test_none_level_runs_directly(self):
        """SandboxLevel.NONE should just run the command directly on host."""
        executor = SandboxExecutor(runtimes={})
        result = executor.run("echo hello", sandbox=SandboxLevel.NONE)
        assert result.ok
        assert "hello" in result.stdout

    def test_toolbox_without_runtime_errors(self):
        executor = SandboxExecutor(runtimes={})
        result = executor.run("echo test", sandbox=SandboxLevel.TOOLBOX)
        assert not result.ok
        assert "not installed" in result.stderr

    def test_podman_without_runtime_errors(self):
        executor = SandboxExecutor(runtimes={})
        result = executor.run("echo test", sandbox=SandboxLevel.LOCKED)
        assert not result.ok
        assert "not installed" in result.stderr


# --- Codex Matrix with containers ---

class TestCodexMatrixContainers:
    def test_matrix_has_container_fields(self):
        """At least some entries should have container tool mappings."""
        with_containers = [m for m in MATRIX if m.container_tool]
        assert len(with_containers) >= 10

    def test_container_upgrades_exist(self):
        """Some PARTIAL entries should be upgraded to FULL with containers."""
        upgraded = [
            m for m in MATRIX
            if m.equivalence_with_containers != m.equivalence
        ]
        assert len(upgraded) >= 5

    def test_no_none_upgrades_to_full(self):
        """NONE should upgrade to at most PARTIAL (not FULL) with containers."""
        for m in MATRIX:
            if m.equivalence == Equivalence.NONE:
                assert m.equivalence_with_containers != Equivalence.FULL, \
                    f"{m.codex_tool}: NONE shouldn't jump to FULL"

    def test_summary_with_containers_is_better(self):
        shell_only = summary(with_containers=False)
        with_containers = summary(with_containers=True)
        assert with_containers["FULL"] >= shell_only["FULL"]
        assert with_containers["PARTIAL"] <= shell_only["PARTIAL"]

    def test_container_upgrade_description_not_empty(self):
        for m in MATRIX:
            if m.container_tool:
                assert m.container_upgrade, f"{m.codex_tool}: has container_tool but no container_upgrade"

    def test_request_permissions_upgraded(self):
        perm = next(m for m in MATRIX if m.codex_tool == "request_permissions")
        assert perm.equivalence == Equivalence.PARTIAL
        assert perm.equivalence_with_containers == Equivalence.FULL

    def test_update_plan_upgraded(self):
        plan = next(m for m in MATRIX if m.codex_tool == "update_plan")
        assert plan.equivalence == Equivalence.PARTIAL
        assert plan.equivalence_with_containers == Equivalence.FULL


# --- Agent container tool dispatch ---

class TestAgentContainerDispatch:
    def setup_method(self):
        self.agent = ShellGeniusAgent(AgentContext(dry_run=True))

    def test_container_create_dispatch(self):
        result = self.agent.handle_tool_call(
            "container_create",
            {"name": "test-box", "runtime": "toolbox"},
        )
        assert "intent" in result
        assert result["intent"] == "CONTAINER_CREATE"

    def test_container_state_dispatch_list_all(self):
        result = self.agent.handle_tool_call(
            "container_state",
            {},
        )
        assert "intent" in result
        assert result["intent"] == "CONTAINER_STATE"

    def test_container_lifecycle_dispatch(self):
        result = self.agent.handle_tool_call(
            "container_lifecycle",
            {"name": "test-box", "action": "stop"},
        )
        assert "intent" in result
        assert result["intent"] == "CONTAINER_LIFECYCLE"

    def test_podman_raw_dispatch(self):
        result = self.agent.handle_tool_call(
            "podman_raw",
            {"subcommand": "version"},
        )
        assert "intent" in result

    def test_toolbox_raw_dispatch(self):
        result = self.agent.handle_tool_call(
            "toolbox_raw",
            {"subcommand": "list --containers"},
        )
        assert "intent" in result

    def test_container_tools_in_as_tools(self):
        tools = self.agent.as_tools()
        tool_names = {t["name"] for t in tools}
        assert "container_create" in tool_names
        assert "container_exec" in tool_names
        assert "container_sandbox_run" in tool_names
        assert "container_state" in tool_names
        assert "container_lifecycle" in tool_names
        assert "podman_raw" in tool_names
        assert "toolbox_raw" in tool_names

    def test_total_tool_count(self):
        tools = self.agent.as_tools()
        # 7 shell + 7 container + 3 dispatch = 17
        assert len(tools) == 17
