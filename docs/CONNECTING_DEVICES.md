# Connecting devices

> LEGACY: these steps belong to the earlier design, in which a controller connects inbound to an SSH server on the device. New computers join with pairing and a node agent that only dials out; see [NETWORK_CONTRACT.md](NETWORK_CONTRACT.md).

The harness performs none of these steps and never touches credentials. Enrollment gives it no access by itself: access exists only because you configured keys and services on the device first. Do these, then enroll (or edit `targets.yaml` by hand).

After reading `inventory_report.md` (`python -m envrec inventory --store runs/<run>`), decide which devices you own and want to use, set one up below, and add it to `targets.yaml`.

## ssh (Linux, macOS, Raspberry Pi, a VM)

1. On the device, turn on the SSH server (Linux: `sudo systemctl enable --now ssh`; macOS: System Settings, General, Sharing, Remote Login).
2. On your controller machine, make a key just for this: `ssh-keygen -t ed25519 -f ~/.ssh/envrec_laptop_b` (empty or non-empty passphrase is your choice; use an agent if you set one).
3. Put the public key on the device: `ssh-copy-id -i ~/.ssh/envrec_laptop_b.pub user@host`.
4. Confirm you can log in with it: `ssh -i ~/.ssh/envrec_laptop_b user@host true`. This also stores the host key; the harness checks host keys strictly by default.
5. Export the path where the harness runs: `export LAPTOP_B_KEY_PATH=$HOME/.ssh/envrec_laptop_b`. Use that variable's name as `key_env`.
6. The device needs `python3` (the worker is a pure-Python archive). The harness installs no system packages.

## ssh on Android (Termux)

1. Install Termux, then `pkg install openssh python`.
2. Set a password for Termux's user with `passwd` only to get started, add your public key to `~/.ssh/authorized_keys`, then turn password login off.
3. Start the server with `sshd` (Termux listens on port 8022 by default; put that in `ssh.port`). It does not start on its own; run it each session or the planner will report unreachable and say so.
4. Phones get a thinner role: the controller keeps the loop and the phone only runs probes and the model server.

## adb_usb (Android over a cable)

1. Settings, About phone, tap Build number seven times; then Developer options, turn on USB debugging.
2. Connect the cable and accept the "Allow USB debugging?" prompt on the phone.
3. `adb devices` should list the serial number. Put it in `adb_usb.serial`.
4. Note: an `adb_usb` runner is not built in this phase; the planner can choose it and report, but running commands over it is a later step.

## serial (a board with only a serial port)

1. Connect the adapter. Find the port: macOS `ls /dev/tty.usb*` or `/dev/cu.*`; Linux `ls /dev/ttyUSB* /dev/ttyACM*`.
2. Make sure your user may open it (Linux: join the `dialout` group).
3. Put the path and the baud rate in `serial`.
4. Note: a `serial` runner is not built in this phase either.

## docker_exec (a local container, for testing)

1. Start the container yourself: `docker run -d --name lab-b python:3.12-slim sleep infinity`.
2. Put the container name in `docker_exec.container`. No key is involved.
3. This is what the Docker test fixtures use, and it is the quickest way to try the whole flow safely.

## First real second machine

The Docker path is fully tested, including real SSH to a container running sshd. Real SSH to a second physical machine is a manual step for you. Start with `inventory`, then enroll one device, then `deploy` a small model and `undo` it, before trusting it with more.
