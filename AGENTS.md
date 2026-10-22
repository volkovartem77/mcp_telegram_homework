# Development Instructions

## Scope and Approval

- Develop strictly in small, user-approved increments.
- Treat `SPEC.md` as planned scope, never as authorization to implement everything.
- Implement only the explicitly requested step. Never proceed automatically to the next step.
- Keep changes minimal, understandable, and independently testable.
- Do not introduce unnecessary abstractions, dependencies, speculative features, or infrastructure.
- Ask before making consequential architectural decisions. Record unresolved design questions in the Open Questions section of `SPEC.md` rather than inventing decisions.
- Wait for the user's manual verification and explicit approval before proceeding.

## Safety and Reliability

- Never access Telegram or perform live API operations without explicit authorization.
- Never expose credentials, login codes, 2FA passwords, API hashes, or session files to logs or LLM context.
- Prefer deterministic behavior and explicit errors over silent fallbacks.
- Never silently convert Telegram errors into empty successful results.
- Enforce safety-critical restrictions in code, not merely in agent instructions.
- Preserve the boundary in `SPEC.md`: research planning, semantic interpretation, iterative context expansion, and research stopping decisions belong to external agents and their skills.

## Language and Step Completion

- Write all documentation, instructions, code comments, docstrings, and MCP tool descriptions in English.
- After each implementation step, summarize changes, tests performed (or explicitly state that none were run), manual verification instructions, and remaining limitations.
- Stop after the authorized step and wait for user review and explicit approval.
