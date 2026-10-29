# ZXQ-1 environment sensor: serial protocol

The ZXQ-1 sends one ASCII frame per reading, terminated by CR LF:

    ~~ZXQ <temperature>/<humidity> ~~

`<temperature>` is whole degrees Celsius. `<humidity>` is whole percent relative humidity.
Both are decimal integers. There is a single space after ZXQ and before the closing tildes.
The device accepts no commands.
