"""Declarative recipes: the write steps of a workspace self-setup as DATA in a skill, run by one generic executor.

The recipe says which commands to run on the target, in which shell, with which undo; the executor shows each exact command, records its undo first, asks the owner, runs it with a hard
timeout, and refuses anything that is not in the recipe whose hash the owner approved. Nothing here knows an operating system: that knowledge is the recipe's, written by a human or
proposed by the model and approved once by hash.
"""
