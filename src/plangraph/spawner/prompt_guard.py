"""Caller-brief admission, not repository context/diffs or review-seat prompts.

Review seats are deliberately unchecked: their prompts embed raw diffs which
may legitimately contain control bytes. Non-string/empty admission is unchanged.
"""
import unicodedata


def admit_prompt(prompt, refuse):
    """Return the input, or raise the entry's refusal without echoing its text."""
    if isinstance(prompt, str):
        line, previous = 1, ''
        for char in prompt:
            if unicodedata.category(char) == 'Cc' and char not in '\t\n\r':
                raise refuse(f'prompt contains control U+{ord(char):04X} on line {line}')
            if char == '\r' or (char == '\n' and previous != '\r'):
                line += 1
            previous = char
    return prompt
