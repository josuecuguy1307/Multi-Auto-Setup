"""The worker release: what runs on an enrolled device. Standard library only.

Deliberately absent from this package (and so from the built artifact): deploy, enroll, the connection
planner, the remote runner, and any network scanning or discovery code. A worker cannot deploy anywhere
because the code to do so is not in it.
"""

__version__ = "2.0.0"
