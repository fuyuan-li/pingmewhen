from __future__ import annotations


def normalize_display_name(value: str) -> str:
    name = value.strip()
    if name and name.islower() and all(character.isalpha() or character in " -'" for character in name):
        return name.title()
    return name
