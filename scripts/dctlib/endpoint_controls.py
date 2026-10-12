"""Enforce Disconnect and byte-rate limits before Docker's port forwarding."""
import ipaddress
import json
from .endpoints import resolved


def render(infos):
    endpoints = [endpoint for info in infos if 'interfaces' in info for endpoint in resolved(info).values()]
    lines = ['table inet betterdocker_endpoints {']
    for index, endpoint in enumerate(endpoints):
        if endpoint.get('rate') and not endpoint.get('link_down'):
            rate = max(1, int(float(endpoint['rate']) * 1048576))
            for direction in ('in', 'out'):
                lines.append(f'chain rate_{direction}_{index} {{ limit rate over {rate} bytes/second burst {max(rate, 65536)} bytes drop; }}')
    for hook, field, priority in (('prerouting', 'daddr', -150), ('output', 'daddr', -150), ('postrouting', 'saddr', 110)):
        lines.append(f'chain {hook} {{ type filter hook {hook} priority {priority}; policy accept;')
        for index, endpoint in enumerate(endpoints):
            addresses = [endpoint['address']] if endpoint.get('address') else []
            if endpoint.get('cidr6'):
                addresses.append(str(ipaddress.IPv6Interface(endpoint['cidr6']).ip))
            for address in addresses:
                family = 'ip6' if ipaddress.ip_address(address).version == 6 else 'ip'
                match = f'{family} {field} {address}'
                if hook == 'prerouting' and endpoint.get('gw' if family == 'ip' else 'gw6'):
                    from .endpoint_network import route_table
                    table = route_table(endpoint, allocate=False)
                    if table is not None:
                        lines.append(f'ct direction reply ct original {family} daddr {address} meta mark set ' + str(table))
                if endpoint.get('link_down'):
                    lines.append(match + ' drop')
                elif endpoint.get('rate'):
                    # IPv4 and IPv6 enter the same bucket for this interface.
                    direction = 'out' if hook == 'postrouting' else 'in'
                    lines.append(match + f' jump rate_{direction}_{index}')
        lines.append('}')
    lines.append('}')
    return '\n'.join(lines) + '\n'


def apply(infos):
    import firewall
    from . import projects as w
    rules = render(infos)
    previous = firewall.STATE / 'endpoints.nft'
    import subprocess
    if not previous.exists() or previous.read_text() != rules or subprocess.run(['nft', 'list', 'table', 'inet', 'betterdocker_endpoints'], capture_output=True).returncode:
        firewall.apply_rules(rules, table='betterdocker_endpoints', cache='endpoints.nft')
    published = [binding for info in infos for binding in firewall.published_ports(info)]
    ipv6 = [binding for binding in published if len(binding) == 5 and ipaddress.ip_address(binding[4]).version == 6]
    apply_host_exceptions(ipv6)
    if not w.node_runtime.load().rootless:
        return
    mappings = []
    for binding in ipv6:
        protocol, port, target, internal, address = binding
        if protocol not in ('tcp', 'udp'):
            raise ValueError('Rootless IPv6 published endpoints support TCP and UDP')
        mappings.append({'address': address, 'port': port, 'protocol': protocol, 'target': target, 'internal': internal})
    path = w.ROOT.parent / 'ipv6-proxy.json'
    w.write_json(path, mappings)
    unit = 'betterdocker-ipv6-proxy.service'
    if mappings:
        active = w.run(['systemctl', 'is-active', unit], check=False, capture_output=True)
        if active.returncode:
            w.run(['systemctl', 'reset-failed', unit], check=False, capture_output=True)
            w.run(['systemd-run', '--unit=' + unit, '--collect', '--property=Restart=on-failure', '--property=UMask=0077',
                   '/usr/bin/python3', str(w.HELPERS / 'ipv6_proxy.py')], capture_output=True)
    else:
        w.run(['systemctl', 'stop', unit], check=False, capture_output=True)


def host_exceptions(bindings):
    # Published IPv6 sockets terminate on the host. Guest NFT policy runs first;
    # only these sockets bypass PVE's separate node INPUT policy.
    return sorted({f'-A BD6-PUBLISHED -d {address}/128 -p {protocol} -m {protocol} --dport {port} -j ACCEPT'
                   for protocol, port, target, internal, address in bindings})


def apply_host_exceptions(bindings):
    import firewall
    import subprocess
    rules = host_exceptions(bindings)
    native = subprocess.run(['nft', '-j', 'list', 'table', 'inet', 'proxmox-firewall'], capture_output=True, text=True)
    if native.returncode == 0:
        apply_native_host_exceptions(bindings, json.loads(native.stdout))
        return
    existing = subprocess.run(['ip6tables', '-w', '-S', 'BD6-PUBLISHED'], capture_output=True, text=True)
    hook = ['-A INPUT -j BD6-PUBLISHED']
    input_rules = firewall.command(['ip6tables', '-w', '-S', 'INPUT']).splitlines()[1:]
    actual = existing.stdout.splitlines()[1:] if existing.returncode == 0 else None
    if actual == rules and input_rules[:1] == hook:
        return
    lines = ['*filter', ':BD6-PUBLISHED - [0:0]', '-F BD6-PUBLISHED', *rules]
    if hook[0] in input_rules:
        lines.append('-D INPUT -j BD6-PUBLISHED')
    lines.extend(['-I INPUT 1 -j BD6-PUBLISHED', 'COMMIT'])
    firewall.command(['ip6tables-restore', '--wait', '--noflush'], input='\n'.join(lines) + '\n')


def apply_native_host_exceptions(bindings, state):
    """DCT sockets pass guest policy before the native host INPUT/OUTPUT policy."""
    import firewall
    tag = 'betterdocker-published-v6'
    import hashlib
    fingerprint = tag + ':' + hashlib.sha256(json.dumps(sorted(set(bindings))).encode()).hexdigest()[:16]
    commands = []
    for direction in ('input', 'output'):
        chain = 'betterdocker-published-' + direction
        chains = [entry['chain'] for entry in state['nftables'] if 'chain' in entry]
        rules = [entry['rule'] for entry in state['nftables'] if 'rule' in entry]
        current = [rule for rule in rules if rule['chain'] == chain]
        if any(not rule.get('comment', '').startswith(tag + ':') for rule in current):
            raise ValueError('Native DCT exception chain belongs to another administrator')
        hooks = [rule for rule in rules if rule['chain'] == direction and rule.get('comment') == tag]
        base = [rule for rule in rules if rule['chain'] == direction]
        if len(current) == len(set(bindings)) and all(rule.get('comment') == fingerprint for rule in current) and len(hooks) == 1 and base and base[0] == hooks[0]:
            continue
        if any(item['name'] == chain for item in chains):
            commands.append('flush chain inet proxmox-firewall ' + chain)
        else:
            commands.append('add chain inet proxmox-firewall ' + chain)
        for protocol, port, target, internal, address in sorted(set(bindings)):
            selector = 'ip6 ' + ('daddr' if direction == 'input' else 'saddr') + ' ' + str(ipaddress.IPv6Address(address))
            selector += ' ' + protocol + (' dport ' if direction == 'input' else ' sport ') + str(port)
            commands.append('add rule inet proxmox-firewall ' + chain + ' ' + selector + ' accept comment "' + fingerprint + '"')
        hooks = [rule for rule in rules if rule['chain'] == direction and rule.get('comment') == tag]
        for hook in hooks:
            commands.append('delete rule inet proxmox-firewall ' + direction + ' handle ' + str(hook['handle']))
        commands.append('insert rule inet proxmox-firewall ' + direction + ' jump ' + chain + ' comment "' + tag + '"')
    if not commands:
        return
    firewall.command(['nft', '--check', '-f', '-'], input='\n'.join(commands) + '\n')
    firewall.command(['nft', '-f', '-'], input='\n'.join(commands) + '\n')
