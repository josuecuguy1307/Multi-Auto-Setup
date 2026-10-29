#!/bin/sh
# Writes the authorized key from $PUBKEY (a test key), then runs sshd in the foreground.
mkdir -p /home/owner/.ssh && printf '%s\n' "$PUBKEY" > /home/owner/.ssh/authorized_keys
chown -R owner /home/owner/.ssh && chmod 700 /home/owner/.ssh && chmod 600 /home/owner/.ssh/authorized_keys
exec /usr/sbin/sshd -D -e -o PasswordAuthentication=no -o PermitRootLogin=no
