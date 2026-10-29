---
name: net-wifi
description: Use when the current Wi-Fi link of this machine, or how crowded the nearby Wi-Fi channels are, matters.
---

# Wi-Fi link and nearby networks

Reports whether the machine is on Wi-Fi, on which channel and band, the signal and noise, and how many other networks are on each channel. Nearby networks are only COUNTED by channel: their names are never stored, because a neighbor's network name is a stranger's data.

macOS hides the network name of the current connection unless Location Services is allowed for the program that runs the harness. When it is hidden the result is a typed `permission` unknown carrying the exact Settings step, not a guess and not a failure. Tell the human the step; do not retry.

Signal numbers move on their own, so they are recorded as volatile facts and never judged wrong for changing.
