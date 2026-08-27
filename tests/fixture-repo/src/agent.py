"""Fixture source for the collector determinism test. Not real code; nothing here runs."""
import anthropic

TRIAGE_MODEL = "claude-sonnet-4-5"

client = anthropic.Anthropic()


def triage(ticket: str) -> str:
    # Requires approval: a support lead approves anything marked urgent.
    response = client.messages.create(model=TRIAGE_MODEL, max_tokens=256, messages=[
        {"role": "user", "content": ticket},
    ])
    return response.content[0].text
