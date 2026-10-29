ENDPOINT = "http://localhost:11434/v1"


def call(prompt, base_url="https://api.example.invalid/v1"):
    return base_url, prompt


def client():
    return dict(host="10.0.0.5", port=8080)
