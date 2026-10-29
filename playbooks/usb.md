# Playbook: usb (an attached USB-serial device)

Goal: find out what this device is, cheapest step first. Never claim more than you have tested.

1. `read_usb_descriptor` and `read_serial_ident` are harmless reads. Do them first.
2. `lookup_usb_ids` and `match_serial_protocol` turn what you read into guesses (hypotheses). A guess is not a fact yet.
3. `confirm_hypotheses` runs ONE more harmless read to check the guesses. If it agrees a fact becomes tested. If it disagrees the fact becomes contradicted. Both are correct outcomes.
4. A vendor match names the vendor, not the model. Leave the model unknown if the list only had the vendor.
5. If every tool is used and something is still unknown, do not guess. Use `ask_human` with the fact key and a clear question.
6. Reply `done` once you have asked or nothing is left.

Pick tools only from the MENU. One JSON object per reply.
