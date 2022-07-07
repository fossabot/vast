#!/usr/bin/env bash

set -e

ABSOLUTE_DIRNAME=$(dirname $(readlink -f  $0))

cd $ABSOLUTE_DIRNAME/../cloud/aws/

export VAST_VERSION=latest
export VAST_AWS_REGION=eu-west-1

export VASTCLOUD_REBUILD=1

./vast-cloud pro.login

unset VASTCLOUD_REBUILD

./vast-cloud pro.pull-image

[[ $(docker ps -f "name=vast-pro" --format '{{.Names}}') == vast-pro ]] || \
   docker run --name vast-pro --rm -d tenzir/vast-pro:latest start

echo "Starting ssh tunnel on port 5000 and 50000"
./vast-cloud pro.key | ssh -N -L 5000:localhost:5000 -L 50000:localhost:50000 ubuntu@51.91.208.2
