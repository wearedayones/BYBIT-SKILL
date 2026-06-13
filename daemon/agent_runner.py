"""
agent_runner.py — universal agent CLI dispatcher.

This repo is a skill. The AI agent that loads it uses its own CLI and handles
its own authentication. The daemon's only job is to call that CLI with the
pipeline prompt.

Configuration (config.yaml → agent):
    command: [list of argv parts]   # required; use {prompt} as placeholder
    timeout_sec: 600                # optional; falls back to invoke_timeout_sec

The {prompt} placeholder in command is replaced with the pipeline prompt at
invocation time. Everything else in argv is passed through verbatim.

Example configs (config.yaml):

    # Claude Code
    agent:
      command: ["claude", "-p", "{prompt}", "--allowedTools", "mcp__bybit__*",
                "Read", "Write", "Edit", "WebFetch", "WebSearch", "Task",
                "--max-turns", "30", "--output-format", "json"]

    # Any other agent CLI
    agent:
      command: ["myagent", "run", "--headless", "{prompt}"]

Only one field matters: command. No agent-type names, no presets, no API keys.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Default command used when config.yaml has no agent.command.
# Change this if Claude Code is not your agent — or just set agent.command.
_DEFAULT_COMMAND: list[str] = [
    "claude", "-p", "{prompt}",
    "--allowedTools", "mcp__bybit__*", "Read", "Write", "Edit",
    "WebFetch", "WebSearch", "Task",
    "--max-turns", "30",
    "--output-format", "json",
]


class AgentRunner:
    """Run the configured agent CLI with the pipeline prompt.

    Usage:
        runner = AgentRunner(cfg)       # cfg = parsed config.yaml dict
        out, rc = runner.run(prompt)    # returns (stdout_text, returncode)

    Raises TimeoutError when the process exceeds timeout_sec. The caller
    treats a timeout as "an order may already be placed — do not retry".
    """

    def __init__(self, cfg: dict):
        agent_cfg = cfg.get("agent", {})
        command = agent_cfg.get("command", _DEFAULT_COMMAND)

        if not isinstance(command, list) or not command:
            raise ValueError(
                "config.yaml → agent.command must be a non-empty list of "
                'strings with "{prompt}" marking where the prompt is inserted.\n'
                "Example:\n"
                '  command: ["myagent", "run", "--headless", "{prompt}"]'
            )
        if not any("{prompt}" in str(part) for part in command):
            raise ValueError(
                'agent.command must contain a "{prompt}" placeholder so the '
                "daemon can pass the pipeline prompt to your agent CLI."
            )

        self.command: list[str] = [str(p) for p in command]
        self.timeout: int = int(
            agent_cfg.get("timeout_sec",
                          cfg.get("invoke_timeout_sec", 600))
        )

    def run(self, prompt: str) -> tuple[str, int]:
        """Substitute {prompt} and run the agent CLI. Returns (output, rc)."""
        cmd = [part.replace("{prompt}", prompt) for part in self.command]
        try:
            result = subprocess.run(
                cmd, cwd=ROOT, capture_output=True, text=True,
                timeout=self.timeout,
            )
            return result.stdout or result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            raise TimeoutError(
                f"Agent CLI timed out after {self.timeout}s. "
                f"Command: {cmd[0]!r}"
            )
        except FileNotFoundError:
            return (
                f"ERROR: agent CLI not found: {cmd[0]!r}. "
                "Is it installed and on PATH? "
                "Check config.yaml → agent.command.",
                1,
            )
