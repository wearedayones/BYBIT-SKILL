"""
agent_runner.py — universal AI agent invocation layer.

Abstracts the AI backend so the trading daemon can call any agent framework,
not just Claude Code. Backend is selected via config.yaml → agent.backend.

Supported backends:
  claude_code        — Claude Code CLI  (claude -p <prompt>)
  openai             — OpenAI API  (ChatGPT / o-series / Codex)
  openai_compatible  — any OpenAI-compatible HTTP endpoint
                       (Ollama, LM Studio, Hermes, OpenClaw, …)
  http               — fully generic HTTP POST  (body_template configurable)

All backends return (stdout: str, returncode: int) so the caller
(candle_watcher.py) stays unchanged.
"""

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _resolve_env(value: str) -> str:
    """Expand $VAR and ${VAR} from the process environment."""
    return os.path.expandvars(str(value))


def _dot_get(obj, path: str):
    """Traverse a dot-separated path like 'choices.0.message.content'."""
    for part in path.split("."):
        if obj is None:
            return None
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return obj


class AgentRunner:
    """Invoke an AI agent using the configured backend.

    Usage:
        runner = AgentRunner(cfg)           # cfg = parsed config.yaml dict
        out, rc = runner.run(prompt)        # returns (text, 0) on success
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        agent_cfg = cfg.get("agent", {})
        self.backend = agent_cfg.get("backend", "claude_code")
        self.backend_cfg = agent_cfg.get(self.backend, {})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, prompt: str, system_prompt: str | None = None) -> tuple[str, int]:
        """Dispatch to the configured backend. Returns (output_text, returncode)."""
        if self.backend == "claude_code":
            return self._run_claude_code(prompt)
        if self.backend in ("openai", "openai_compatible"):
            return self._run_openai(prompt, system_prompt)
        if self.backend == "http":
            return self._run_http(prompt, system_prompt)
        raise ValueError(
            f"Unknown agent backend: {self.backend!r}. "
            "Valid values: claude_code, openai, openai_compatible, http"
        )

    # ------------------------------------------------------------------
    # Claude Code CLI backend
    # ------------------------------------------------------------------

    def _run_claude_code(self, prompt: str) -> tuple[str, int]:
        bcfg = self.backend_cfg
        exe = bcfg.get("executable", "claude")
        allowed_tools: list[str] = bcfg.get("allowed_tools", [
            "mcp__bybit__*", "Read", "Write", "Edit",
            "WebFetch", "WebSearch", "Task",
        ])
        max_turns = bcfg.get("max_turns", self.cfg.get("max_turns", 30))
        output_format = bcfg.get("output_format", "json")
        timeout = bcfg.get("timeout_sec",
                           self.cfg.get("invoke_timeout_sec", 600))

        cmd = [exe, "-p", prompt, "--allowedTools"] + allowed_tools + [
            "--max-turns", str(max_turns),
            "--output-format", output_format,
        ]

        try:
            result = subprocess.run(
                cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout
            )
            return result.stdout or result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            raise TimeoutError(
                f"Claude Code timed out after {timeout}s"
            )

    # ------------------------------------------------------------------
    # OpenAI / OpenAI-compatible backend
    # ------------------------------------------------------------------

    def _run_openai(self, prompt: str, system_prompt: str | None) -> tuple[str, int]:
        bcfg = self.backend_cfg
        api_key = os.environ.get(bcfg.get("api_key_env", "OPENAI_API_KEY"), "")
        if not api_key:
            return (
                f"ERROR: env var '{bcfg.get('api_key_env', 'OPENAI_API_KEY')}' "
                "not set — cannot call OpenAI backend.",
                1,
            )

        base_url = bcfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
        model = bcfg.get("model", "gpt-4o")
        max_tokens = bcfg.get("max_tokens", 16000)
        temperature = bcfg.get("temperature", 0)
        timeout = bcfg.get("timeout_sec",
                           self.cfg.get("invoke_timeout_sec", 600))

        # Resolve system prompt: file takes precedence over passed-in text
        sys_text = self._load_system_prompt(bcfg, system_prompt)

        messages = []
        if sys_text:
            messages.append({"role": "system", "content": sys_text})
        messages.append({"role": "user", "content": prompt})

        payload = json.dumps({
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }).encode()

        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        return self._http_call(req, timeout, "choices.0.message.content")

    # ------------------------------------------------------------------
    # Generic HTTP backend  (Hermas, OpenClaw, custom endpoints)
    # ------------------------------------------------------------------

    def _run_http(self, prompt: str, system_prompt: str | None) -> tuple[str, int]:
        bcfg = self.backend_cfg
        url = _resolve_env(bcfg.get("url", ""))
        if not url:
            return "ERROR: agent.http.url is not configured.", 1

        timeout = bcfg.get("timeout_sec",
                           self.cfg.get("invoke_timeout_sec", 600))

        sys_text = self._load_system_prompt(bcfg, system_prompt)

        body_template = bcfg.get("body_template")
        if body_template:
            escaped_prompt = json.dumps(prompt)[1:-1]
            escaped_system = json.dumps(sys_text or "")[1:-1]
            body = body_template \
                .replace("{prompt}", escaped_prompt) \
                .replace("{system}", escaped_system)
            body_bytes = body.encode()
        else:
            messages = []
            if sys_text:
                messages.append({"role": "system", "content": sys_text})
            messages.append({"role": "user", "content": prompt})
            body_bytes = json.dumps({
                "messages": messages,
                "prompt": prompt,
            }).encode()

        headers = {"Content-Type": "application/json"}
        for k, v in bcfg.get("headers", {}).items():
            headers[k] = _resolve_env(v)

        req = urllib.request.Request(
            url, data=body_bytes, headers=headers, method="POST"
        )
        response_field = bcfg.get("response_field", "output")
        return self._http_call(req, timeout, response_field)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_system_prompt(self, bcfg: dict, fallback: str | None) -> str:
        """Return text from system_prompt_file (relative to ROOT) or fallback."""
        sys_file = bcfg.get("system_prompt_file")
        if sys_file:
            sys_path = ROOT / sys_file
            if sys_path.exists():
                return sys_path.read_text()
        return fallback or ""

    @staticmethod
    def _http_call(req, timeout: int, response_field: str) -> tuple[str, int]:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read())
                text = _dot_get(body, response_field)
                if text is None:
                    text = json.dumps(body)
                return str(text), 0
        except urllib.error.HTTPError as e:
            err = e.read().decode(errors="replace")
            return f"HTTP {e.code}: {err}", 1
        except Exception as e:
            return str(e), 1
