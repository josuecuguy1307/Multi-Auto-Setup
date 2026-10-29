---
name: protocol-instrument
description: Use when the target is a measuring instrument or a device that answers command queries and its model is not yet known.
---

# A lab instrument

Instruments are polite and standard: most answer `*IDN?` with maker, model, serial and firmware. That one query is the whole probe. Anything that changes a setting is out of scope.

## reach
Find the port or the address. A network instrument must be on the owner's subnet.

## identify
Send only the fixed identification query, after approval, and stop at the first reply.

## match
Read the reply as data. The serial number is an identifier: it is hashed and never stored, only the maker and the model are.

## ask
If the reply is not a standard format, record the unknown and ask.
