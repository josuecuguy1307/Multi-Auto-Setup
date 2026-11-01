CHANNELS = {
    "panel": Channel("panel", "the owner panel", False, False, ("panel_click",)),
    "claude-desktop": Channel("claude-desktop", "Claude Desktop", True, True, ("host_elicitation", "os_dialog")),
    "openwebui": Channel("openwebui", "Open WebUI", False, False, ("host_dialog", "os_dialog")),
}
