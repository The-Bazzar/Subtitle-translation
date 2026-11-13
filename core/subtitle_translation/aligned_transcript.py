"""Map MFA word spans back to the unchanged adopted text."""

from __future__ import annotations

import re


def _key(text: str, tokenize) -> str:
    return "".join(tokenize(text)).replace("'", "")


def map_display_words(text: str, words: list[dict], tokenize) -> list[dict]:
    atoms = []
    leading = 0
    for match in re.finditer(r"\S+", text):
        key = _key(match.group(), tokenize)
        if not key:
            if atoms:
                atoms[-1]["end"] = match.end()
            continue
        atoms.append({"start": leading if not atoms else match.start(), "end": match.end(), "key": key})
    mapped = []
    left = right = 0
    while left < len(atoms) and right < len(words):
        atom_start, word_start = left, right
        source_key = atoms[left]["key"]
        aligned_key = _key(words[right]["word"], tokenize)
        if not aligned_key:
            raise ValueError("MFA word has no source mapping")
        while source_key != aligned_key:
            if source_key.startswith(aligned_key) and right + 1 < len(words):
                right += 1
                aligned_key += _key(words[right]["word"], tokenize)
            elif aligned_key.startswith(source_key) and left + 1 < len(atoms):
                left += 1
                source_key += atoms[left]["key"]
            else:
                raise ValueError("MFA words do not map to the adopted draft")
        start, end = atoms[atom_start]["start"], atoms[left]["end"]
        mapped.append({"word": text[start:end].strip(), "start": words[word_start]["start"],
                       "end": words[right]["end"], "source_range": [start, end]})
        left += 1
        right += 1
    if left != len(atoms) or right != len(words) or not mapped:
        raise ValueError("incomplete MFA source mapping")
    return mapped
