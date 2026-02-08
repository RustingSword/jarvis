# HEARTBEAT

@run_always

## Quick checks (lightweight only)
- Check last ~200 lines of ~/.jarvis/jarvis.log for ERROR/WARNING patterns.
- Check runtime status via ./scripts/run.sh status (if available).
- Look for recent task failures in the log window (last 1-2 hours).
- If current time is after 09:00 local and no report exists at reports/nightly-build/YYYY-MM-DD.md, flag it.

## Insight Radar
- Run: python3 /home/nazgul/jarvis/scripts/radar_run.py --mode heartbeat
- If output contains HEARTBEAT_OK, reply with one sentence containing HEARTBEAT_OK.
- Otherwise, forward the script output as the response (should include send_to_user for PDF).

## Response rules
- If no owner action needed, reply with one sentence containing HEARTBEAT_OK.
- If owner action is needed, briefly state reason, impact, and suggested next step.
- Keep the response short; avoid heavy analysis.

## Guardrails
- Do NOT modify config files or secrets.
- Do NOT run heavy scans or wide history analysis (reserve for nightly build).
- Do NOT restart services or post externally.
