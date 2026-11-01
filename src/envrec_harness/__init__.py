"""The Harness App: how the owner works with the harness from a chat he already uses.

This package holds what every chat shares and nothing that decides: the `intent` schema (what the harness says), the card layer (self-contained HTML built from an intent's data) and the
per-host wrappers around those cards. Which AI answers, what a run does and who may decide are the coordinator's (`envrec_coordinator.harness`); an adapter only translates."""
