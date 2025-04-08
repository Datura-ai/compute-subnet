#!/bin/bash
set -eux -o pipefail

source ./docker_build.sh

echo "$DOCKERHUB_PAT" | docker login -u "$DOCKERHUB_USERNAME" --password-stdin
docker push "$IMAGE_NAME"

docker rmi "$IMAGE_NAME"
docker builder prune -f