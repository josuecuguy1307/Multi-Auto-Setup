CHANNELS = {
    "claude-desktop": Channel("claude-desktop", "Claude Desktop", True, True, ("panel_click", "host_elicitation")),
    "some-cloud-chat": Channel("some-cloud-chat", "A cloud chat", True, False, ("panel_click",)),
    "agent-chat": Channel("agent-chat", "A chat with a model in the middle", False, True, ("host_dialog",)),
}
