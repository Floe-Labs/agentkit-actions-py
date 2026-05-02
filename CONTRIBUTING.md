# Contributing to floe-agentkit-actions

Thanks for your interest in contributing to Floe's Python AgentKit ActionProvider.

## Getting Started

```bash
git clone https://github.com/Floe-Labs/agentkit-actions-py.git
cd agentkit-actions-py
pip install -e ".[dev]"
```

## Development

```bash
pytest                    # Run tests
pytest --cov              # With coverage
ruff check src/           # Lint
ruff format src/           # Format
```

## Pull Requests

1. Fork the repo and create your branch from `main`
2. If you've added code, add tests
3. Ensure `pytest` passes
4. Write a clear PR description explaining the change

## Code Style

- Python 3.10+
- Type hints on all public functions
- Pydantic models for schemas
- Follow existing patterns in `action_provider.py`

## Reporting Bugs

Open a GitHub issue with:
- Steps to reproduce
- Expected vs actual behavior
- Python version and OS

## Security Issues

See [SECURITY.md](SECURITY.md) — do **not** open a public issue for security vulnerabilities.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
