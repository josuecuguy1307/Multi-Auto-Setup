---
name: protocol-signal
description: Use when the target is a signal or a stream of symbols, such as a recording, whose encoding is not yet known.
---

# A radio or electrical signal

Receiving is allowed; transmitting is not. Work from a recording so the analysis can be repeated and checked.

## capture
Read the recorded file. Its hash is its identity.

## describe
Count frames, look for a header and a checksum, look at the character set. Simple measurements first.

## match
Compare the shape with known signatures. A match is a hypothesis with the signature named.

## research and ask
Documentation can settle a doubt; it is untrusted text. If the encoding stays unknown, record it as unknown and ask.
