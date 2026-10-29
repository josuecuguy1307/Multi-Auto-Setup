# KRL-3 data logger: serial protocol

The KRL-3 says nothing on its own. It answers the query `$I` with one ASCII line terminated by CR LF:

    KRL3 R<firmware> b=<battery> t=<temperature> m=<mode>

`<firmware>` is the firmware revision, for example 4.2. `<battery>` is the battery charge in whole percent.
`<temperature>` is the temperature in TENTHS of a degree Celsius, as a whole number (342 means 34.2 degrees).
`<mode>` is a code: 0 off, 1 idle, 2 standby, 3 active.
The device accepts no other commands.
