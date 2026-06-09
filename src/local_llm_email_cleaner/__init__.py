"""local-llm-email-cleaner: parse a Gmail Takeout MBOX, classify locally with
an LLM, review proposals, and apply approved actions through the Gmail API."""


def main() -> None:
    from .cli import main as cli_main

    cli_main()
