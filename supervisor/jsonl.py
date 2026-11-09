def jsonl_lines(text: str, *, keepends: bool = False) -> list[str]:
    """Split JSONL on LF only: str.splitlines breaks JSON strings containing U+2028, U+2029 or U+0085.

    Preserve blank lines and omit the empty item after a final LF, as str.splitlines does.
    With keepends, retain the exact text of each line for JSONL rewrites.
    """
    lines = text.split("\n")
    if keepends:
        return [line + "\n" for line in lines[:-1]] + ([lines[-1]] if lines[-1] else [])
    if lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]
