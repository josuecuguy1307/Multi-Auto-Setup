---
name: os-info
description: Use when the operating system build, kernel string, locale or boot time of this machine is needed beyond the basic OS name and version.
---

# OS build, kernel, locale and boot time

The basic probes already give the OS name and version. This skill adds what distinguishes two machines running the same release: the build, the kernel string, the locale and when the machine booted.

Read it when you need to tell builds apart, when a fact depends on the kernel, or when a boot time matters. Do not read it twice: the answer does not change during a run.

On macOS the boot time is kept as a boot epoch (seconds since 1970), not as an uptime, because an uptime is a number that is wrong a second later; a caller derives uptime against its own clock. On Linux the uptime comes straight from `/proc/uptime`.

A missing locale is simply not reported: `defaults` may be absent in a minimal install, and an absent fact is better than an invented one.
