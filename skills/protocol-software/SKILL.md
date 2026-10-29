---
name: protocol-software
description: Use when the target is a program, an application folder or a repository to be understood by reading it, never by running it.
---

# A program or a repository

Read it, do not run it. Static analysis is enough to say what a program is, and running unknown code is the one thing here that cannot be undone.

## identify
Hash the file and name its format. The hash is the identity: the same bytes are the same program wherever they are.

## analyze
Use the tool that fits the kind: a folder of JavaScript has a package and modules; a Mach-O binary has architecture, libraries, imports and exports. Each tool reports what it did not check. An empty list with incomplete coverage is not proof of absence: write it as unknown, never as false.

## summarize
Say what you are sure of, what you suspect and what was not checked. Only paths under the roots the human configured can be analyzed.
