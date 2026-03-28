# GitSense

GitSense is a Python CLI that uses a local Ollama model to generate a conventional commit message from your git diff, lets you review or edit it, then commits and pushes your current branch.

## Features

- Generates commit messages using a local LLM (Ollama + llama3)
- Enforces conventional commit style (`feat`, `fix`, `chore`, `refactor`, `docs`)
- Lets you accept the generated message by pressing Enter or edit it before commit
- Commits and pushes the current branch
- Supports staged-only mode with `--staged`

## Prerequisites

- Python 3.10+
- Git
- Ollama installed and running locally
- `llama3` model pulled in Ollama

## 1) Install Ollama

Install Ollama from the official site:

- https://ollama.com/download

After installation, verify it is available:

```bash
ollama --version
```

Start Ollama server if it is not already running:

```bash
ollama serve
```

In another terminal, pull the model used by GitSense:

```bash
ollama pull llama3
```

Quick connectivity check:

```bash
curl http://localhost:11434/api/tags
```

If this returns JSON, your local Ollama server is ready.

## 2) Install GitSense

From this project directory:

```bash
python3 -m pip install .
```

For local development (editable install):

```bash
python3 -m pip install -e .
```

## 3) Make the `gitsense` Command Available

If `gitsense` is not found after install, add your Python scripts directory to `PATH`.

Example (macOS with python.org installer):

```bash
echo 'export PATH="/Library/Frameworks/Python.framework/Versions/3.15/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

Then verify:

```bash
gitsense --help
```

## 4) Usage

Run in any git repository with changes:

```bash
gitsense
```

Staged-only diff mode:

```bash
gitsense commit --staged
```

What happens when you run it:

1. Reads your git diff
2. Sends a trimmed diff to Ollama (`llama3`)
3. Generates a commit message
4. Prompts you to accept/edit the message
5. Runs:
	- `git add .`
	- `git commit -m "<message>"`
	- `git push` (or `git push -u origin <current-branch>` when needed)

## 5) Troubleshooting

### `command not found: gitsense`

- Ensure the package is installed in the same Python environment you are using.
- Add the Python scripts directory to your `PATH`.

### `Failed to contact Ollama at http://localhost:11434/api/generate`

- Start Ollama with `ollama serve`.
- Confirm port `11434` is accessible.

### `model not found` or empty LLM output

- Run `ollama pull llama3`.
- Retry command.

### `No changes detected in git diff`

- Modify files, or stage files and use `gitsense commit --staged`.

### Git push fails due to permissions/auth

- Verify your git remote and authentication (SSH key or token).

## Publish (Optional)

To publish as a package:

```bash
python3 -m pip install --upgrade build twine
python3 -m build
python3 -m twine upload dist/*
```

Before publishing, update project metadata in `pyproject.toml` (author, repository URLs, and version).

## License

MIT
