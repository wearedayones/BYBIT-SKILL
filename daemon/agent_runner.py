"""
agent_runner.py — universal AI agent invocation layer.

This repo is a SKILL: it is consumed by an AI agent framework that the user
already runs and is already authenticated with (Claude Code, Codex, Gemini
CLI, OpenCode, OpenClaw, Hermas, ...). No API keys live here — each agent
CLI handles its own auth.

The daemon only needs to hand the candle-close prompt to that agent's
headless/non-interactive CLI. Which CLI is used is set in
config.yaml → agent.backend, and each backend is just a command template
where "{prompt}" is replaced with the pipeline prompt.

All backends return (stdout: str, returncode: int) so the caller
(candle_watcher.py) stays unchanged.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Built-in command templates for known agent CLIs. Every entry is a plain
# argv list; "{prompt}" is substituted at invocation time. Override any of
# these (or add your own) via config.yaml → agent.<backend>.command.
PRESETS: dict[str, list[str]] = {
    # Claude Code headless mode
    "claude_code": [
        "claude", "-p", "{prompt}",
        "--allowedTools", "mcp__bybit__*", "Read", "Write", "Edit",
        "WebFetch", "WebSearch", "Task",
        "--max-turns", "30",
        "--output-format", "json",
    ],
    # OpenAI Codex CLI non-interactive mode
    "codex": ["codex", "exec", "--full-auto", "{prompt}"],
    # Google Gemini CLI headless mode
    "gemini": ["gemini", "--yolo", "-p", "{prompt}"],
    # OpenCode non-interactive run
    "opencode": ["opencode", "run", "{prompt}"],
    # Aider scripting mode
    "aider": ["aider", "--yes", "--message", "{prompt}"],
    # "custom" has no preset — define agent.custom.command in config.yaml
    # for any other framework (OpenClaw, Hermas, in-house agents, ...).
}


class AgentRunner:
    """Invoke the configured agent CLI with the pipeline prompt.

    Usage:
        runner = AgentRunner(cfg)           # cfg = parsed config.yaml dict
        out, rc = runner.run(prompt)        # returns (text, 0) on success

    Raises TimeoutError when the agent exceeds timeout_sec — the caller
    treats a timeout as "an order may exist, do not retry".
    """

    def __init__(self, cfg: dict):
        agent_cfg = cfg.get("agent", {})
        self.backend = agent_cfg.get("backend", "claude_code")
        bcfg = agent_cfg.get(self.backend, {}) or {}

        self.command: list[str] | None = bcfg.get("command") or PRESETS.get(self.backend)
        if not self.command:
            raise ValueError(
                f"Agent backend {self.backend!r} has no command template. "
                f"Built-in presets: {', '.join(PRESETS)}. For anything else, "
                "set agent.<backend>.command in config.yaml (a list of argv "
                'parts where "{prompt}" marks where the prompt goes).'
            )
        if not any("{prompt}" in part for part in self.command):
            raise ValueError(
                f"agent.{self.backend}.command must contain a \"{{prompt}}\" "
                "placeholder so the daemon can pass the pipeline prompt."
            )

        self.timeout = bcfg.get("timeout_sec",
                                agent_cfg.get("timeout_sec",
                                              cfg.get("invoke_timeout_sec", 600)))

    def run(self, prompt: str) -> tuple[str, int]:
        cmd = [part.replace("{prompt}", prompt) for part in self.command]
        try:
            result = subprocess.run(
                cmd, cwd=ROOT, capture_output=True, text=True,
                timeout=self.timeout,
            )
            return result.stdout or result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            raise TimeoutError(
                f"Agent CLI ({self.backend}) timed out after {self.timeout}s"
            )
        except FileNotFoundError as e:
            return (
                f"ERROR: agent CLI executable not found: {e}. Is "
                f"'{cmd[0]}' installed and on PATH? (backend: {self.backend})",
                1,
            )
