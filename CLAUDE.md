# CLAUDE.md

This file provides guidance for AI assistants (Claude, etc.) working in this repository.

## Repository Overview

- **Name:** anthropic
- **Status:** New project (early stage)
- **Default branch:** `main`

## Project Structure

```
.
├── CLAUDE.md        # AI assistant guidelines (this file)
└── README.md        # Project readme
```

This repository is in its initial state. Update this section as the project grows to reflect the actual directory layout and module boundaries.

## Development Workflow

### Git Conventions

- **Default branch:** `main`
- **Branch naming:** Use descriptive prefixes — `feature/`, `fix/`, `refactor/`, `docs/`, `test/`
- **Commit messages:** Write concise, imperative-mood messages (e.g., "Add user authentication endpoint", not "Added user authentication endpoint")
- Keep commits atomic — one logical change per commit
- Do not force-push to `main`

### Before Committing

- Ensure any new code compiles/builds without errors
- Run the full test suite if one exists
- Run linters/formatters if configured

## Coding Conventions

### General Principles

- Keep code simple and readable; avoid over-engineering
- Follow existing patterns when extending the codebase
- Prefer explicit over implicit behavior
- Write tests for new functionality
- Do not introduce secrets, credentials, or API keys into source control

### Style

- Follow the language-specific style guide adopted by this project (to be established as code is added)
- Use consistent naming conventions throughout the codebase
- Keep functions/methods focused on a single responsibility

## AI Assistant Guidelines

When working in this repository:

1. **Read before writing** — Always read relevant files before proposing changes
2. **Stay focused** — Only make changes that are directly requested or clearly necessary
3. **Don't over-engineer** — Avoid adding abstractions, utilities, or features beyond what is asked
4. **Preserve conventions** — Match the style and patterns already present in the codebase
5. **Test your work** — Run available tests and builds after making changes
6. **Ask when uncertain** — If requirements are ambiguous, ask for clarification rather than guessing
7. **No secrets** — Never commit `.env` files, credentials, or API keys

## Dependencies & Tooling

_To be updated as the project adds a package manager, build system, and toolchain._

## Testing

_To be updated when a test framework is configured._

## Common Tasks

_To be updated as the project defines standard development tasks (build, test, lint, deploy, etc.)._
