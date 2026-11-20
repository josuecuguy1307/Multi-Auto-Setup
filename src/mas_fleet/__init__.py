"""Fleet join for the MAS portable device (D-036). Not part of the gate: the gate (mas_device) never touches the network; this package
opens the one TCP connection the host's owner asked for and drives the gate exactly like the model does, so every write still needs a
press."""
