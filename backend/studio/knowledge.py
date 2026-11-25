"""Which MCP generation a pack belongs to."""


def classify(info):
    pack = info['pack']
    num = info.get('mcp_number') or 0
    if info['engine'] == 'legacy':
        if pack in ('revengpack', 'revengpack11', 'revengpack12', 'revengpack13'):
            return 'reveng'
        if pack.startswith('revengpack'):
            return 'rgs'
        return 'mcp2'
    if num < 5:
        return 'mcp3'
    if num < 6:
        return 'mcp5'
    if num < 7:
        return 'mcp6'
    if num < 8:
        return 'mcp7'
    if num < 9:
        return 'mcp8'
    return 'mcp9'
