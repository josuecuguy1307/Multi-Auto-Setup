#!/bin/sh
# Writes the authorized key from $PUBKEY (a test key), then runs sshd in the foreground.
mkdir -p /home/josue/.ssh && printf '%s\n' "$PUBKEY" > /home/josue/.ssh/authorized_keys
chown -R josue /home/josue/.ssh && chmod 700 /home/josue/.ssh && chmod 600 /home/josue/.ssh/authorized_keys
exec /usr/sbin/sshd -D -e -o PasswordAuthentication=no -o PermitRootLogin=no
