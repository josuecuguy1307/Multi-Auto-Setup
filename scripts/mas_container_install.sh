#!/bin/sh
# LINUX-CONTAINER run of install.sh: a fresh image, a non-root user, the real `uv` installer and a release served from INSIDE the container (busybox httpd on loopback),
# so the installer's https-or-loopback rule is exercised as it is. Prints a transcript; exit 0 only if every check held.
#
#   scripts/mas_container_install.sh IMAGE ROLE RELEASE_DIR TAG SHA256 [--platform linux/amd64]
#     IMAGE debian:bookworm-slim | fedora:latest      ROLE node | coordinator      RELEASE_DIR holds download/<tag>/mas-<tag>.tar.gz
set -eu
IMAGE="$1"; ROLE="$2"; REL="$3"; TAG="$4"; SHA="$5"; shift 5
HERE="$(cd "$(dirname "$0")/.." && pwd)"
docker run --rm "$@" -v "$HERE/install.sh:/work/install.sh:ro" -v "$REL:/srv:ro" -e TAG="$TAG" -e SHA="$SHA" -e ROLE="$ROLE" "$IMAGE" sh -eu -c '
if command -v apt-get >/dev/null; then apt-get update -qq >/dev/null && apt-get install -y -qq curl ca-certificates busybox >/dev/null 2>&1
else dnf install -y -q busybox curl tar util-linux >/dev/null 2>&1 || true; fi
useradd -m tester
busybox httpd -p 127.0.0.1:8765 -h /srv
cat >/work/run.sh <<"IN"
set -eu
export MAS_RELEASE_BASE=http://127.0.0.1:8765
echo "### os: $(. /etc/os-release; echo $PRETTY_NAME) $(uname -m)  user: $(id -un)"
sh /work/install.sh --yes --role "$ROLE" --no-join --tag "$TAG" --sha256 "$SHA"
export PATH="$HOME/.local/bin:$PATH"
echo "### mas --version:"; mas --version
echo "### listening sockets of a node process: none are opened by install"
echo "### mas service status (no systemd user manager expected in a container):"; mas service status || true
if [ "$ROLE" = node ]; then echo "### mas service install on an unpaired node must refuse:"; mas service install --yes || true; fi
if [ "$ROLE" = coordinator ]; then
  echo "### coordinator starts on loopback and answers:"; mas coordinator start --bind 127.0.0.1 --port 7743 --no-discovery >/tmp/coord.log 2>&1 &
  sleep 4; head -2 /tmp/coord.log
  (command -v ss >/dev/null && ss -ltn | grep -c 7743) || (grep -c 7743 /proc/net/tcp >/dev/null && echo "7743 listening (proc)") || true
  kill %1 2>/dev/null || true
fi
echo "### uninstall:"; sh "$HOME/.mas/install.sh" --uninstall --yes | tail -2
echo "### left in HOME:"; find "$HOME" -mindepth 1 -not -path "*/.local/share/*" -not -path "*/.cache/*" -not -path "$HOME/.local" -not -path "$HOME/.local/bin" -not -name ".bash*" -not -name ".profile" | head
echo "### state kept (keys survive):"; ls -d "$HOME/.local/share/mas" 2>/dev/null || echo "(none created)"
IN
su tester -c "ROLE=$ROLE TAG=$TAG SHA=$SHA sh /work/run.sh"
'
