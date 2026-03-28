from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Sequence

import requests
import typer

app = typer.Typer(
    help="Auto commit message generator powered by a local Ollama model.",
    invoke_without_command=True,
)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Run commit flow by default if no subcommand is provided."""
    if ctx.invoked_subcommand is None:
        commit_command(staged=False)

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3"
DIFF_CHAR_LIMIT = 8000
FILE_LIST_LIMIT = 40
STAT_CHAR_LIMIT = 2000
FALLBACK_COMMIT = "chore: update project files"

PROMPT_TEMPLATE = """You are a principal engineer writing high-quality conventional commits.

Analyze the repository changes and produce the single best conventional commit.

Rules:
- Allowed types: feat, fix, chore, refactor, docs
- Message must be concise, specific, imperative mood, and lower-case start
- Message body (after type:) must be <= 72 characters
- Avoid vague words like "update", "changes", "auto", "misc"
- Prefer describing user-impacting or developer-impacting intent
- If mostly docs files changed, use docs
- If mostly cleanup/reorganization changed, use refactor
- If fixing failures/bugs/errors, use fix

Return ONLY valid JSON:
{{"type":"<type>","message":"<subject without type prefix>"}}

Changed files:
{files}

Diff stats:
{stats}

Patch excerpt:
{diff}
"""


@dataclass(frozen=True)
class GeneratedCommit:
    commit: str


@dataclass(frozen=True)
class ChangeContext:
    diff: str
    files: list[str]
    diff_stats: str


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



def get_changed_files(staged: bool) -> list[str]:
    """Return changed file paths for current diff selection."""
    cmd = ["git", "diff", "--cached", "--name-only"] if staged else ["git", "diff", "--name-only"]
    result = run_cmd(cmd)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def get_diff_stats(staged: bool) -> str:
    """Return compact git diff stats output."""
    cmd = ["git", "diff", "--cached", "--stat"] if staged else ["git", "diff", "--stat"]
    result = run_cmd(cmd)
    return result.stdout.strip()


def get_change_context(staged: bool) -> ChangeContext:
    """Collect patch + metadata so the model can produce better commit messages."""
    diff_text = get_git_diff(staged=staged)
    files = get_changed_files(staged=staged)
    stats = get_diff_stats(staged=staged)
    return ChangeContext(diff=diff_text, files=files, diff_stats=stats)


def generate_commit(context: ChangeContext, timeout_seconds: float = 45.0) -> str:
    """Send diff to Ollama and return raw response text from the model."""
    trimmed_diff = context.diff[:DIFF_CHAR_LIMIT]
    files_text = "\n".join(context.files[:FILE_LIST_LIMIT]) or "(no files)"
    stats_text = (context.diff_stats or "(no stats)")[:STAT_CHAR_LIMIT]
    prompt = PROMPT_TEMPLATE.format(diff=trimmed_diff, files=files_text, stats=stats_text)

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "top_p": 0.9,
        },
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



def _normalize_subject(subject: str, max_len: int) -> str:
    cleaned = " ".join(subject.strip().split())
    cleaned = cleaned.strip("\"'` ")
    cleaned = cleaned.rstrip(".")
    cleaned = re.sub(r"^(add|added)\s+", "add ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(update|updated)\s+", "improve ", cleaned, flags=re.IGNORECASE)

    if cleaned:
        cleaned = cleaned[0].lower() + cleaned[1:]

    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" ,;:-")

    return cleaned


def _sanitize_commit(commit_message: str) -> str:
    candidate = " ".join(commit_message.strip().split())
    candidate = candidate.strip("\"'`")
    allowed_types = "feat|fix|chore|refactor|docs"
    pattern = re.compile(rf"^({allowed_types}):\s+.+$", re.IGNORECASE)
    if not pattern.match(candidate):
        return FALLBACK_COMMIT

    commit_type, _, rest = candidate.partition(":")
    commit_type = commit_type.lower()
    subject = _normalize_subject(rest.strip(), max_len=72)
    if not subject:
        return FALLBACK_COMMIT
    return f"{commit_type}: {subject}"


def _fallback_commit_from_context(context: ChangeContext) -> str:
    """Create a deterministic commit message when model output is malformed."""
    files = context.files
    file_l = [f.lower() for f in files]
    docs_only = bool(file_l) and all(
        f.endswith(".md") or f.startswith("docs/") for f in file_l
    )

    joined = "\n".join(file_l) + "\n" + context.diff.lower()
    if docs_only:
        return "docs: refresh setup and usage documentation"
    if re.search(r"\b(fix|bug|error|exception|fail|broken)\b", joined):
        return "fix: resolve issues in recent code updates"
    if re.search(r"\b(refactor|rename|cleanup|reorganize)\b", joined):
        return "refactor: improve internal code structure"
    if any(f in {"pyproject.toml", "requirements.txt"} for f in file_l):
        return "chore: update project tooling and dependencies"
    if any(f.endswith("cli.py") or "/cli" in f for f in file_l):
        return "feat: improve cli commit generation workflow"
    return FALLBACK_COMMIT



def parse_output(raw_output: str) -> GeneratedCommit:
    """Extract COMMIT from model text with safe fallback parsing."""
    json_match = re.search(r"\{[\s\S]*\}", raw_output)
    if json_match:
        try:
            payload = json.loads(json_match.group(0))
            if isinstance(payload, dict):
                commit_type = str(payload.get("type", "")).strip().lower()
                message = str(payload.get("message", "")).strip()
                candidate = f"{commit_type}: {message}"
                sanitized = _sanitize_commit(candidate)
                return GeneratedCommit(commit=sanitized)
        except (ValueError, TypeError):
            pass

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
        context = get_change_context(staged=staged)
    except CommandError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    if not context.diff.strip():
        typer.echo("No changes detected in git diff. Nothing to commit.")
        raise typer.Exit(code=0)

    if len(context.diff) > DIFF_CHAR_LIMIT:
        typer.echo(
            f"Diff is larger than {DIFF_CHAR_LIMIT} chars; truncating before sending to Ollama.",
        )

    try:
        model_output = generate_commit(context)
        suggestion = parse_output(model_output)
    except Exception as exc:  # pylint: disable=broad-except
        typer.secho(f"LLM generation failed: {exc}", fg=typer.colors.YELLOW, err=True)
        suggestion = GeneratedCommit(commit=_fallback_commit_from_context(context))

    if suggestion.commit == FALLBACK_COMMIT:
        suggestion = GeneratedCommit(commit=_fallback_commit_from_context(context))

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
