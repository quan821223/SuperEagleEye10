"""Best-effort decoding for subprocess output on Windows consoles with a
non-UTF-8 code page (used by proto generation and WMI device queries)."""


def _decode_shell_output(data: bytes) -> str:
    if not data:
        return ""
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "utf-8", "cp950"):
        try:
            return data.decode(encoding)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")
