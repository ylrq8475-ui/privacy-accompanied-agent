# Codex Project Instructions

## Project goal

This repository implements only the competition demo described in:

- docs/IMPLEMENTATION_PLAN.md

Do not turn it into a general-purpose product.

## Mandatory workflow

1. Read this file and docs/IMPLEMENTATION_PLAN.md before making changes.
2. Work on only the explicitly requested phase.
3. Do not implement future phases.
4. Stop after the current phase is complete.
5. Do not commit or push unless explicitly requested.
6. Before modifying code, inspect the existing repository and relevant interfaces.
7. Prefer the smallest implementation that satisfies the current phase.
8. Run the required tests before reporting completion.
9. Never report a test as passed unless it was actually executed.
10. Never claim a physical action succeeded when only a Mock was used.

## Architecture constraints

- Step3 provides structured state hypotheses and recommendation reasons.
- Step3 must never authorize or execute actions.
- Deterministic policy code controls clarification and authorization.
- Music and AC actions must use independent action_id values.
- external-connector is the only INTERNET egress service.
- LOCAL and LAN calls must not go through external-connector.
- All calls must identify their network scope as LOCAL, LAN, or INTERNET.
- All internet payloads must pass through Privacy Guard.
- The console must display real backend events and real outbound payloads.
- SQLite must not store raw audio or raw video.
- Pending actions must never execute automatically after restart.

## Existing infrastructure restrictions

Do not perform any of the following without explicit approval:

- sudo
- reboot
- shutdown
- docker system prune
- docker volume prune
- docker compose down on existing model services
- docker stop or docker restart on StepAudio or Step3 services
- kill, pkill, or killall model processes
- change existing model startup parameters
- download additional model weights
- expose new public ports
- modify SSH configuration
- modify secrets or real .env files

Do not modify existing StepAudio or Step3 containers.

Use their existing HTTP interfaces through adapters.

## Privacy restrictions

Never read, print, commit, or upload:

- SSH private keys
- API tokens
- real .env files
- real user audio
- real user video
- private memory databases
- model weight files
- private credentials
- complete public IP or SSH credentials in documentation

Use synthetic Demo data for tests.

## Required completion report

At the end of every phase, report:

1. Work completed
2. Files added or modified
3. Commands executed
4. Tests executed
5. Exact test results and sample counts
6. Unresolved issues
7. Risks
8. Rollback procedure
9. Suggested next phase

Then stop and wait for review.