# Audius read-only Demo modes

The competition Demo keeps two Audius states independent.

## Verified seed preview

This is the default mode. It enables public preview requests only for IDs loaded
from `data/music/seeds/audius_mood_playlist_20_tracks.resolved.json`.

```text
SPARK_AUDIUS_ENABLED=true
SPARK_AUDIUS_SEED_CONFIG=data/music/seeds/audius_mood_playlist_20_tracks.resolved.json
```

Public metadata and preview URL probes were verified without an API key or
Bearer token. Optional credentials may still be supplied through process
environment or secret files if Audius changes provider requirements. They must
not be committed, logged, returned by health endpoints, or stored in the Track
Catalog.

## Optional online playlist sync

Online synchronization is disabled unless the sync overlay and a local public
playlist mapping are supplied explicitly:

```bash
export SPARK_AUDIUS_CONFIG_DIR=/absolute/path/to/read-only-config
docker compose \
  -f docker-compose.dgx.yml \
  -f docker-compose.dgx.audius.yml \
  -f docker-compose.dgx.audius-sync.yml \
  up -d external-connector
```

The config directory must contain `audius_playlists.local.json`. Successful
playlist validation adds its provider track IDs to the connector's in-memory
allowlist. Seed preview remains available when online sync is absent or fails.

## Seventeen-track acceptance

Run inside the `external-connector` container after its health response reports
`preview_ready=true`:

```bash
python scripts/verify_audius_seed_previews.py \
  --base-url http://127.0.0.1:8030 \
  --output /tmp/audius-seed-preview-report.json
```

Acceptance requires `sample_count=17`, `success_count=17`, and
`failure_count=0`. The connector validates and decodes every preview before
returning it. The script discards the bytes and sets
`physical_speaker_verified=false`; a separate audible Demo check is still
needed if a real playback device is part of the presentation.

## Privacy boundary

Only `external-connector` performs INTERNET requests. The outbound business
payload remains `action + logical track_id`; source audio, emotion history,
routine data, recommendation reasons, credentials, provider URLs, and audio
bytes are not written to the audit log or SQLite.
