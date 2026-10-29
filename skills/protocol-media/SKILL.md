---
name: protocol-media
description: Use when the goal is to connect an already-paired Bluetooth device on this computer, make it the audio output and play a track from the owner's own music app.
---

# Connect a paired device and play something through it

This computer already has the owner's devices paired; this protocol never pairs a new one. Each effect asks the owner first with the exact action, and each can be undone: the device is disconnected, the previous audio output is restored.

## paired
Read the list of paired devices. Each shows its type and name in FACTS. The earbuds are the paired headphones or headset the goal means; if none is paired, say so (ask the human), do not guess.

## connect
Choose the device from FACTS and connect it. Give its fact key as `device`.

## output
Read the audio outputs (`audio_state`) so the current one is on the record and can be put back, then make the device the output so the sound goes to it and not to the speakers of the computer.

## find
Find the track in the owner's own library (read-only, with the owner's permission). Only the chosen track's id and title are kept.

## play
Play the chosen track. Volume stays capped.

## check
Read the player's state so it is on the record. Whether the goal was reached is checked afterwards by code, from the world.
