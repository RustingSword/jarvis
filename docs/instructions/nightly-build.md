# Nightly Build Instructions

Purpose: deliver one low-risk improvement or a clear report during the nightly window.

Read-first
- Read MEMORY.md.
- If a recent daily note exists under memory/, skim the latest one.
- Check ~/.jarvis/jarvis.log (last ~200 lines, more if needed) for errors or warnings.
- If docs/owner_profile.md exists, skim it for current rules and gaps.

Scope rules
- Do NOT modify config files or secrets.
- Do NOT delete files, restart services, or post externally.
- Do NOT touch production deployments.
- Only text edits to docs/owner_profile.md and reports/ are allowed without confirmation.
- If a change is needed, propose it and ask for confirmation.

Allowed outputs
- A concise Nightly Report in chat.
- Optionally write a report file under reports/nightly-build/YYYY-MM-DD.md.
- Optionally update docs/owner_profile.md with explicit, high-confidence rules (include evidence).

Suggested nightly focus
- Summarize key errors/warnings in the log window.
- Extract new owner preferences/constraints from memory/ and stage candidates.
- Update owner profile only when evidence is explicit; otherwise mark pending-confirm in the report.
- Propose trigger candidates (no config changes).

Nightly Report template
- Task (or No-op)
- Rationale
- Evidence/Signals
- Owner profile updates (yes/no + list)
- Trigger candidates (if any)
- Proposed next step (needs approval: yes/no)
- Verification/rollback plan
