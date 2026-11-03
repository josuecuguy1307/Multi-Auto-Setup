"""Every word `mas` with no arguments shows the owner: the first-run menu. Fixed product text, English."""

MENU_TITLE = "This computer"
MENU_INTRO = "This menu sets up only this computer. Adding computers, pairing and models happen later, in your chat."
ROLE_ASK = "Is this computer the coordinator (the one you talk to and that pairs the others) or a node (one of the others)?"
ROLE_COORDINATOR = "Coordinator"
ROLE_NODE = "Node"
ROLE_FOUND = "This computer is a {role} (found on disk)."
ROLE_NOTE = "Choosing writes nothing: it only decides which steps are shown."
STEP_LABELS = {
    "menu_step_service_coordinator": "Keep the coordinator running as a service (starts when you sign in)",
    "menu_step_service_node": "Keep the node running as a service (starts when you sign in)",
    "menu_step_connect": "Connect Claude Desktop to Multi Auto Setup",
    "menu_step_join": "Announce this node on the network (find a coordinator and pair)",
}
STATE_DONE = "done"
STATE_TODO = "not done"
STATE_BLOCKED = "waiting for: {needs}"
CHOOSE = "Type a number, or nothing to leave: "
ALL_DONE = "Everything on this menu is done. Next: open your chat and ask it to add a computer."
STEP_SHOW = "\nThis step runs:\n  command: {command}\n  undo:    {undo}\nThe command shows its own plan and asks you to type yes; nothing is written before that."
ADDRESS_ASK = "This computer's address on your network (the coordinator listens on it):"
ADDRESS_NONE = "No private-network address was found on this computer. Connect it to your network first. Nothing was changed."
ADDRESS_CHOOSE = "Type a number: "
CANCELLED = "Nothing was changed."
STEP_RESULT_OK = "Step finished."
STEP_RESULT_FAIL = "That step did not finish (code {code}). Nothing else was changed by the menu."
MENU_RESTART_CLAUDE = "Quit Claude Desktop completely (Cmd+Q on a Mac) and open it again so it picks up the connection."
LEFT = "Left the menu. Run `mas` again any time."
NOT_A_TERMINAL = "mas needs a terminal to show its menu. Here is what it can do:"
NEEDS_NOTE = "Do the earlier step first."

CONFIG_SHOW = "Verbosity: {level}  (brief, normal or detailed). Change it with: mas config verbosity LEVEL"
CONFIG_SET = "Verbosity is now {level}. It applies to the next thing your chats and this terminal show."
CONFIG_BAD = "Verbosity is one of: {levels}."
CONFIG_UNDO = "Undo: mas config verbosity {old}"
