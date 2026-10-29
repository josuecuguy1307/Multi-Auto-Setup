LOCALHOST = "127.0.0.1"
ENDPOINT = "http://127.0.0.1:11434/v1"
V6 = "http://[::1]:8080/v1"


def health(port):
    return f"http://127.0.0.1:{port}/health"


def call(prompt, host=LOCALHOST, base_url="http://127.0.0.1:11434/v1"):
    return host, base_url, prompt
