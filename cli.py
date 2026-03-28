from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Sequence

import requests
import typer


app = typer.Typer(help="Auto commit message generator powered by a local Ollama model.")


@app.callback()
def main() -> None:
    """Root callback to keep explicit subcommand invocation."""

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3"
DIFF_CHAR_LIMIT = 8000
FALLBACK_COMMIT = "chore: update project files"

PROMPT_TEMPLATE = """You are a senior software engineer.

Analyze the following git diff and generate a conventional commit message.
Allowed types: feat, fix, chore, refactor, docs.

Format strictly:
COMMIT: <type>: <message>

Git diff:
{diff}
"""


@dataclass(frozen=True)
class GeneratedCommit:
    commit: str


class CommandError(RuntimeError):
    """Raised when a subprocess command fails."""



def run_cmd(args: Sequence[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a shell command and return completed process details."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise CommandError(f"Failed to run command {' '.join(args)}: {exc}") from exc

    if check and result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        details = stderr or stdout or "No error details available"
        raise CommandError(f"Command failed: {' '.join(args)}\n{details}")

    return result



def get_git_diff(staged: bool) -> str:
    """Return git diff output for staged or unstaged changes."""
    cmd = ["git", "diff", "--cached"] if staged else ["git", "diff"]
    result = run_cmd(cmd)
    return result.stdout



def generate_commit(diff_text: str, timeout_seconds: float = 30.0) -> str:
    """Send diff to Ollama and return raw response text from the model."""
    trimmed_diff = diff_text[:DIFF_CHAR_LIMIT]
    prompt = PROMPT_TEMPLATE.format(diff=trimmed_diff)

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
    }

    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=timeout_seconds)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to contact Ollama at {OLLAMA_URL}: {exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("Ollama response was not valid JSON") from exc

    model_response = data.get("response")
    if not isinstance(model_response, str) or not model_response.strip():
        raise RuntimeError("Ollama response did not include usable text")

    return model_response.strip()



def _sanitize_commit(commit_message: str) -> str:
    candidate = " ".join(commit_message.strip().split())
    allowed_types = "feat|fix|chore|refactor|docs"
    pattern = re.compile(rf"^({allowed_types}):\s+.+$", re.IGNORECASE)
    if not pattern.match(candidate):
        return FALLBACK_COMMIT

    commit_type, _, rest = candidate.partition(":")
    return f"{commit_type.lower()}:{rest}"



def parse_output(raw_output: str) -> GeneratedCommit:
    """Extract COMMIT from model text with safe fallback parsing."""
    commit_match = re.search(r"^\s*COMMIT\s*:\s*(.+)\s*$", raw_output, re.IGNORECASE | re.MULTILINE)

    if commit_match:
        candidate = commit_match.group(1)
    else:
        first_line = raw_output.strip().splitlines()[0] if raw_output.strip() else ""
        candidate = first_line

    return GeneratedCommit(commit=_sanitize_commit(candidate))



def get_current_branch() -> str:
    """Return the current checked-out git branch name."""
    result = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    branch = result.stdout.strip()
    if not branch:
        raise CommandError("Unable to determine current git branch")
    return branch


def run_git_automation(commit_message: str) -> None:
    """Commit all changes and push the current branch."""
    current_branch = get_current_branch()

    commands = [
        ["git", "add", "."],
        ["git", "commit", "-m", commit_message],
    ]

    for cmd in commands:
        typer.echo(f"-> {' '.join(cmd)}")
        run_cmd(cmd)

    typer.echo("-> git push")
    try:
        run_cmd(["git", "push"])
    except CommandError:
        typer.echo(f"No upstream configured. Setting upstream to origin/{current_branch}.")
        run_cmd(["git", "push", "-u", "origin", current_branch])


@app.command("commit")
def commit_command(
    staged: bool = typer.Option(False, "--staged", help="Use staged diff (git diff --cached)."),
) -> None:
    """Generate a commit message from git diff, then commit and push current branch."""
    try:
        diff_text = get_git_diff(staged=staged)
    except CommandError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    if not diff_text.strip():
        typer.echo("No changes detected in git diff. Nothing to commit.")
        raise typer.Exit(code=0)

    if len(diff_text) > DIFF_CHAR_LIMIT:
        typer.echo(
            f"Diff is larger than {DIFF_CHAR_LIMIT} chars; truncating before sending to Ollama.",
        )

    try:
        model_output = generate_commit(diff_text)
        suggestion = parse_output(model_output)
    except Exception as exc:  # pylint: disable=broad-except
        typer.secho(f"LLM generation failed: {exc}", fg=typer.colors.YELLOW, err=True)
        suggestion = GeneratedCommit(commit=FALLBACK_COMMIT)

    typer.echo("\nGenerated commit message:")
    typer.echo(suggestion.commit)

    commit_message = typer.prompt(
        "Press Enter to use this message, or edit it",
        default=suggestion.commit,
        show_default=True,
    )

    try:
        run_git_automation(commit_message)
    except CommandError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.secho("Commit created and current branch pushed successfully.", fg=typer.colors.GREEN)


if __name__ == "__main__":
    try:
        app()
    except KeyboardInterrupt:
        typer.secho("Interrupted by user.", fg=typer.colors.YELLOW, err=True)
        sys.exit(130)
