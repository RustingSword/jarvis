# Nightly Build Instructions

Purpose: deliver one low-risk improvement or a clear report during the nightly window.

Read-first
- Read MEMORY.md.
- If a recent daily note exists under memory/, skim the latest one.
- Check ~/.jarvis/jarvis.log (last ~200 lines, more if needed) for errors or warnings.

Scope rules
- Do NOT modify config files or secrets.
- Do NOT delete files, restart services, or post externally.
- Do NOT touch production deployments.
- If a change is needed, propose it and ask for confirmation.

Allowed outputs
- A concise Nightly Report in chat.
- Optionally write a report file under reports/nightly-build/YYYY-MM-DD.md.

Nightly Report template
- Task (or No-op)
- Rationale
- Evidence/Signals
- Proposed next step (needs approval: yes/no)
- Verification/rollback plan
