"""Bounded persisted output."""
OUTPUT_KEEP = 65536   # bytes of stdout a run record keeps
def keep_output(text, limit=OUTPUT_KEEP):
    """Whole ``text`` up to ``limit`` bytes; beyond it the head (verdict) and the tail, marked."""
    data = text.encode()
    if len(data) <= limit:
        return text
    head = limit // 4
    return (data[:head].decode('utf-8', errors='ignore') + '\n[... output truncated ...]\n'
            + data[-(limit - head):].decode('utf-8', errors='ignore'))
