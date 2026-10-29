# The script contract

```python
from envrec_sbx import Caps, Out, Lookups

def parse(inp):
    caps = Caps(inp)          # caps.get("name") -> a capture: .text .ok .error .detail .json() .lines()
    out = Out(caps)
    c = caps.get("probe")
    if c is None or not c.ok:
        return out.lost(c, "serial.protocol").result()     # an unknown with the typed reason
    line = c.text.strip()
    out.fact("serial.protocol", "nmea", src="probe", quote=line[:40], hypothesis=True)
    return out.result()
```

## Input

`inp["captures"]` maps a capture name to `{text, ok, error, detail}`. A step of the plan produces a capture named by its `id` (the tool may name its own captures; they are listed in the tool's description). `inp["platform"]`, `inp["round"]` and `inp["params"]` are also there.

## Helpers

`serial_text(cap)` returns what a serial device said, whether the capture is the log that `serial_identify` makes (JSON with `passive` and `reply`) or plain text. Use it instead of parsing the log yourself, so a test written with a plain-text capture works the same.
`clean_text(s, cap)` and `mask_ids(s, cap)` clean a world string; `Caps.with_prefix(p)` lists captures by name prefix.

## Output (built by `Out`)

- `out.fact(key, value, src=, quote=, limitations=[], confidence=, hypothesis=False)`
- `out.entity(kind, ring, ident, owned, attrs, resolved=True, src=, quote=, prefix=, links=[])` - `ident` is hashed by the harness and never stored; `owned` is `owned`, `ambient` or `unknown`
- `out.unknown(key, reason, detail)` / `out.lost(capture, *keys)` - `reason` is one of `unavailable`, `denied`, `timeout`, `not_applicable`, `permission`
- `out.want(id, tool, args, reducer=None)` - ask for another read by a registered tool; the script is called again with the answer as a capture named `id`

## Rounds

A script that needs a table lookup or a second read returns only `want` calls the first time. The harness checks each call (registered tool, allowed tier, valid arguments), runs it, and calls the script again with the new captures. At most four rounds and 160 calls. `Lookups(caps).call(id, tool, args)` wraps this: it returns the answer, `{}` if the table is missing, or `None` while the answer is still being fetched.

## Limits

Standard library only. A few seconds. 64 KiB of script. At most 300 facts, 600 entities, 4 MiB of output. Anything else the script prints is ignored.
