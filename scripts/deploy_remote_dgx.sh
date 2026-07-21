#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
    echo "usage: deploy_remote_dgx.sh IMAGE_TAG RELEASE_DIR" >&2
    exit 2
fi

image_tag=$1
release_dir=$2
project_name=${SPARK_COMPOSE_PROJECT_NAME:-spark-active-companion-audius-candidate}

case "$image_tag" in
    spark-active-companion-demo:[a-f0-9][a-f0-9]*) ;;
    *) echo "invalid image tag" >&2; exit 2 ;;
esac
cd "$release_dir"

model_state() {
    for pattern in companion-stepaudio companion-step3-vl; do
        container_name=$(docker ps --filter "name=$pattern" --format '{{.Names}}' | head -n 1)
        if [ -z "$container_name" ]; then
            echo "required model container is unavailable: $pattern" >&2
            return 1
        fi
        docker inspect \
            --format '{{.Name}}|{{.Id}}|{{.State.StartedAt}}|{{.RestartCount}}' \
            "$container_name"
    done
}

model_state > model-state-before.txt

SPARK_DEMO_IMAGE="$image_tag" \
docker compose -p "$project_name" \
    -f docker-compose.dgx.yml \
    -f docker-compose.dgx.audius.yml \
    config --quiet

docker build --target test -t "${image_tag}-test" .
docker run --rm --network none "${image_tag}-test"
docker build --target runtime -t "$image_tag" .

existing_backend=$(docker compose -p "$project_name" -f docker-compose.dgx.yml ps -q backend 2>/dev/null || true)
if [ -n "$existing_backend" ]; then
    docker inspect --format '{{.Config.Image}}' "$existing_backend" > previous-image.txt
fi

SPARK_DEMO_IMAGE="$image_tag" \
docker compose -p "$project_name" \
    -f docker-compose.dgx.yml \
    -f docker-compose.dgx.audius.yml \
    up \
    -d --no-build external-connector track-catalog backend

attempt=0
backend_id=$(SPARK_DEMO_IMAGE="$image_tag" docker compose -p "$project_name" -f docker-compose.dgx.yml ps -q backend)
until docker exec "$backend_id" python - <<'PY'
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2) as response:
    if response.status != 200:
        raise SystemExit(1)
PY
do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        echo "backend container health did not become ready" >&2
        exit 1
    fi
    sleep 2
done

model_state > model-state-after.txt
if ! cmp -s model-state-before.txt model-state-after.txt; then
    echo "existing model container state changed during deployment" >&2
    exit 1
fi

SPARK_DEMO_IMAGE="$image_tag" \
python3 scripts/verify_dgx_deployment.py \
    --compose-file docker-compose.dgx.yml \
    --project "$project_name" \
    > deployment-verification.json

printf '%s\n' "$image_tag" > deployed-image.txt
printf '%s\n' "DEPLOYED_IMAGE=$image_tag"
printf '%s\n' "REMOTE_PUBLISHED_PORT=NONE"
printf '%s\n' "VERIFICATION=$release_dir/deployment-verification.json"
