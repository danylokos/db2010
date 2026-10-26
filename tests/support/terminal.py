import re

def rendered_text(raw):
    """Render only the CR/LF and clear-row/down sequences emitted by our ticker."""
    text = raw.decode('utf-8')
    lines, column, offset = [''], 0, 0
    while offset < len(text):
        char = text[offset]
        if char == '\033':
            match = re.match(r'\033\[(?:K|J)', text[offset:])
            assert match, repr(text[offset:offset+20])
            lines[-1] = lines[-1][:column]
            offset += len(match[0])
            continue
        if char == '\r':
            column = 0
        elif char == '\n':
            lines.append('')
            column = 0
        else:
            line = lines[-1]
            lines[-1] = line[:column] + char + line[column+1:]
            column += 1
        offset += 1
    return '\n'.join(lines)
