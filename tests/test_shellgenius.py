"""Tests for ShellGenius agent."""

from shellgenius.agent import ShellGeniusAgent, AgentContext, Intent
from shellgenius.engine.pipe_algebra import (
    Pipeline, PipeStage, PipelineError, StreamType,
    chain, explain_pipeline, safely_quote_command, _split_pipeline,
)
from shellgenius.knowledge.corpus import (
    PipePattern, Shell, lookup_idioms, lookup_fd_tricks,
    PIPE_IDIOMS, FD_TRICKS, QUOTING_RULES,
)
from shellgenius.openclaw.skill import generate_manifest, SYSTEM_PROMPT_FRAGMENT
import pytest


# --- Pipe Algebra ---

class TestPipeAlgebra:
    def test_basic_pipeline_render(self):
        p = Pipeline()
        p.add(PipeStage("grep ERROR"))
        p.add(PipeStage("wc -l"))
        assert p.render() == "grep ERROR | wc -l"

    def test_type_checked_composition(self):
        p = Pipeline()
        p.add(PipeStage("find . -print0", output_type=StreamType.NULL_DELIM))
        p.add(PipeStage("xargs -0 grep ERROR", input_type=StreamType.NULL_DELIM))
        assert "xargs -0" in p.render()

    def test_type_mismatch_raises(self):
        p = Pipeline()
        p.add(PipeStage("cat data.json", output_type=StreamType.JSON))
        with pytest.raises(PipelineError, match="Type mismatch"):
            p.add(PipeStage("xargs -0 echo", input_type=StreamType.NULL_DELIM))

    def test_chain_helper(self):
        p = chain(
            PipeStage("ls -la"),
            PipeStage("grep py"),
            PipeStage("wc -l"),
        )
        assert len(p.stages) == 3
        assert p.render() == "ls -la | grep py | wc -l"

    def test_uuoc_warning(self):
        p = Pipeline()
        p.add(PipeStage("cat file.txt"))
        p.add(PipeStage("grep ERROR"))
        warnings = p.validate()
        assert any("UUOC" in w for w in warnings)

    def test_explain_pipeline(self):
        stages = explain_pipeline("grep ERROR | sort | uniq -c | head -10")
        assert len(stages) == 4
        assert stages[0]["tool"] == "grep"
        assert stages[3]["tool"] == "head"

    def test_split_pipeline_respects_quotes(self):
        stages = _split_pipeline("echo 'hello | world' | grep hello")
        assert len(stages) == 2

    def test_safely_quote_command(self):
        safe = safely_quote_command("echo hello world")
        assert safe == "echo hello world"

    def test_safely_quote_catches_bad_quoting(self):
        with pytest.raises(PipelineError):
            safely_quote_command("echo 'unterminated")

    def test_args_quoting(self):
        stage = PipeStage("grep", args=["-r", "hello world", "."])
        assert stage.full_command == "grep -r 'hello world' ."


# --- Knowledge Corpus ---

class TestCorpus:
    def test_idioms_not_empty(self):
        assert len(PIPE_IDIOMS) > 10

    def test_lookup_by_category(self):
        filters = lookup_idioms(category=PipePattern.FILTER)
        assert all(i.category == PipePattern.FILTER for i in filters)
        assert len(filters) >= 2

    def test_lookup_by_shell(self):
        posix_only = lookup_idioms(shell=Shell.POSIX)
        for idiom in posix_only:
            assert Shell.POSIX in idiom.shells

    def test_fd_tricks_not_empty(self):
        assert len(FD_TRICKS) >= 5

    def test_quoting_rules_not_empty(self):
        assert len(QUOTING_RULES) >= 5

    def test_fd_tricks_by_shell(self):
        bash_tricks = lookup_fd_tricks(Shell.BASH)
        assert all(Shell.BASH in t.shells for t in bash_tricks)


# --- Agent ---

class TestAgent:
    def setup_method(self):
        self.agent = ShellGeniusAgent(AgentContext(dry_run=True))

    def test_explain(self):
        resp = self.agent.explain("grep ERROR | sort | uniq -c")
        assert resp.intent == Intent.EXPLAIN_PIPELINE
        assert "grep" in resp.explanation.lower()

    def test_compose_finds_frequency_pattern(self):
        resp = self.agent.compose_pipeline("count frequency sort")
        assert resp.pipeline is not None
        assert "sort" in resp.pipeline or "uniq" in resp.pipeline

    def test_fix_quoting_detects_unquoted_var(self):
        resp = self.agent.fix_quoting("echo $HOME is nice")
        assert any("unquoted" in w.lower() or "word splitting" in w.lower()
                    for w in resp.warnings)

    def test_fd_help_returns_something(self):
        resp = self.agent.fd_help("swap stdout stderr")
        assert resp.explanation
        assert resp.intent == Intent.FD_REDIRECT

    def test_translate_bash_to_posix_warns(self):
        resp = self.agent.translate(
            "diff <(cmd1) <(cmd2)", Shell.BASH, Shell.POSIX
        )
        assert any("NOT" in w for w in resp.warnings)

    def test_run_dry_run(self):
        resp = self.agent.run("echo hello")
        assert resp.exec_result is not None
        assert resp.exec_result.dry_run

    def test_tool_call_dispatch(self):
        result = self.agent.handle_tool_call(
            "shell_explain",
            {"command": "ls | grep py"},
        )
        assert "intent" in result
        assert result["intent"] == "EXPLAIN_PIPELINE"

    def test_unknown_tool_call(self):
        result = self.agent.handle_tool_call("nonexistent", {})
        assert "error" in result


# --- OpenClaw Integration ---

class TestOpenClaw:
    def test_manifest_structure(self):
        m = generate_manifest()
        assert m["id"] == "shellgenius"
        assert "tools" in m
        assert len(m["tools"]) >= 6
        assert m["hardware"]["gpu_required"] is False

    def test_manifest_tools_have_required_fields(self):
        m = generate_manifest()
        for tool in m["tools"]:
            assert "name" in tool
            assert "description" in tool
            assert "parameters" in tool

    def test_system_prompt_exists(self):
        assert "shell_compose" in SYSTEM_PROMPT_FRAGMENT
        assert "pipe" in SYSTEM_PROMPT_FRAGMENT.lower()
