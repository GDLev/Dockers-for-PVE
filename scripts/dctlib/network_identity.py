"""Rootless source identities, independent of optional guest firewall policy."""
import ipaddress
import json
import re


def render(guards):
    lines = []
    for hook in ('input', 'forward'):
        lines.append(f'chain identity_{hook} {{ type filter hook {hook} priority -30; policy accept;')
        for bridges in guards.values():
            for bridge, identity in bridges.items():
                if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', bridge): raise ValueError('Invalid Docker bridge identity')
                ips = [str(ipaddress.IPv4Address(value)) for value in identity.get('ips', [])]
                macs = identity.get('macs', [])
                if any(not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', value) for value in macs): raise ValueError('Invalid Docker MAC identity')
                prefix = 'iifname ' + json.dumps(bridge) + ' '
                lines.append(prefix + ('ip saddr != { ' + ', '.join(ips) + ' } ' if ips else '') + 'drop')
                lines.append(prefix + ('ether saddr != { ' + ', '.join(macs) + ' } ' if macs else '') + 'drop')
                lines.append(prefix + 'meta nfproto ipv6 drop')
        lines.append('}')
    return lines
