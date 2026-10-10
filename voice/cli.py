"""Local tester: chat with the brain from a terminal, no WhatsApp needed.

Only needs OPENROUTER_API_KEY set. Lets us prove the brain loop end-to-end
before the Meta webhook is wired.

Run from the repo root:  .venv/bin/python -m voice.cli
"""
import sys

from . import agent, config


def main():
    if not config.OPENROUTER_API_KEY:
        print("OPENROUTER_API_KEY not set: export it or load voice/.env first.")
        sys.exit(1)
    print(f"brain voice (model: {config.MODEL}), type 'exit' to quit.\n")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if text in ("exit", "quit"):
            break
        if not text:
            continue
        print("brain>", agent.handle("cli", text), "\n")


if __name__ == "__main__":
    main()
