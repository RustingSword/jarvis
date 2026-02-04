# Owner Profile + Proactive Assistant Design

Date: 2026-02-04

## Goals
- Build a stable owner profile document to guide assistant behavior.
- Separate daily facts from long-term rules to reduce noise and improve traceability.
- Move heavy analysis and consolidation into nightly build.
- Keep all changes reversible and safe by default.

## Non-goals
- No automatic config changes, external actions, or paid API calls.
- No direct edits to secrets, deployments, or running services.
- No silent overwrites of existing long-term rules.

## Artifacts
- docs/owner_profile.md: stable, long-term profile and rules.
- memory/YYYY-MM-DD.md: daily factual log with evidence lines.
- reports/nightly-build/YYYY-MM-DD.md: nightly consolidation report.

## Data Capture Rules (Daily)
- Capture only explicit statements from the owner: preferences, constraints, priorities, and rules.
- Append to memory/YYYY-MM-DD.md with tags, no deletions.
- Store the original phrasing when possible for evidence.

Suggested tags:
- preference, constraint, priority, workflow, risk, format, boundary, trigger_candidate

## Owner Profile Structure (Stable)
Recommended sections in docs/owner_profile.md:
1. Identity and role preferences
2. Communication style (format, depth, pace)
3. Decision and risk boundaries
4. Common tasks and cadence
5. Trigger candidate pool (not enabled)
6. Do-not-touch / sensitive areas

Each rule should include:
- statement: normalized rule
- category: one of the tags
- confidence: low/medium/high
- source: date + evidence line
- status: active / pending-confirm / deprecated

## Nightly Build Flow (Heavy Ops)
1. Read MEMORY.md and recent memory/YYYY-MM-DD.md (last 7-14 days).
2. Extract rule candidates and detect conflicts.
3. Produce a change summary (additions, modifications, conflicts).
4. Update docs/owner_profile.md only with high-confidence, non-conflicting rules.
5. Output a Nightly Report with evidence and suggested next steps.

Conflict handling:
- Never auto-overwrite; mark as pending-confirm with both sources.
- Prefer recent evidence but require explicit owner confirmation to change.

## Trigger Candidate Pool
- Store candidates in docs/owner_profile.md with risk tier and benefit.
- Low-risk candidates can be placed into nightly analysis only.
- Medium/high-risk candidates require explicit owner confirmation before editing config.yaml.

## Tooling (Nightly Only)
Candidate scripts (no external dependencies, no config writes):
- scripts/owner_profile_extract.py: extract candidates from memory.
- scripts/trigger_candidate_score.py: score by frequency/impact/risk.
- scripts/health_check_summary.py: summarize errors and pipeline status.

Outputs are written to reports/nightly-build/YYYY-MM-DD.md.

## Error Handling and Verification
- Any failure results in a no-op with a clear Nightly Report entry.
- If input files are missing or malformed, skip consolidation and report.
- All changes are text-only and reversible via git or manual deletion.

## Implementation Phasing
- Phase 1: Create docs/owner_profile.md skeleton and nightly report section.
- Phase 2: Implement extraction and candidate scoring scripts.
- Phase 3: Add conflict detection and evidence-based updates.

## Open Questions
- Default lookback window: 7 or 14 days?
- Confidence thresholds for automatic profile updates?
- Preferred rule categories or exclusions?
