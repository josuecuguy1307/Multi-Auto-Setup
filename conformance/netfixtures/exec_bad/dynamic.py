def run_model_code(text):
    exec(compile(text, "generated", "exec"))
