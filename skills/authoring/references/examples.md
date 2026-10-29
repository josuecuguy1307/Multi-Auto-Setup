# Four finished skills, one for each shape

Open the one whose problem looks like yours. Each has a `SKILL.md` that explains why it is built the way it is, a `scripts/parse.py`, and `tests/` with real captures.

| shape | skill | what to learn from it |
|---|---|---|
| One command, then a parse | `skills/net-listening` | the smallest complete skill: one `run_command` step per platform, a script that reads its output, a positive test per platform and a capture that must not match |
| A table lookup with one confirming read | `skills/usb-devices` | the script reads the device list, then ASKS for a lookup per distinct id (`lookup_usb_id`) in a second round; a vendor match is not a product match; a missing table is its own typed unknown |
| Listen first, then match | `skills/serial-probe` | one exclusive exchange with a serial port (read tier, approval shows the exact queries), then the reply is matched against signatures by a lookup; a match is a hypothesis with a confidence |
| Several rounds of reads, each deciding the next | `skills/net-mdns` | service types, then instances, then hosts, then addresses; reads in each round are asked for by the script and run by the harness; the reducer keeps device ids out of everything |
