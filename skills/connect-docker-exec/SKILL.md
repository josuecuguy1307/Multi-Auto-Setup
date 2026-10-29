---
name: connect-docker-exec
description: Use when a running container is to be reached with docker exec.
---

# Reaching a device: docker_exec

A container is reached through the local docker CLI by its name. The check is a listing of running containers; the container must be running and named in `targets.yaml`. Nothing is installed in the image: the worker goes into the writable layer under the fixed install root and is removed by the undo.

When the route does not answer, tell the human exactly this and stop: _start the container and make sure the docker CLI can see it, then re-run_. The same sentence is kept in `references/setup-hint.txt`, which the planner reads, so the advice the human sees is the advice written here.
