#!/usr/bin/env python3
"""Apply native PVE guest policy to the endpoint and owned Docker bridges."""
import argparse
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import runtime

STATE = Path('/var/lib/betterdocker/firewall')
TABLE = 'betterdocker'


def command(args, **kwargs):
    result = runtime.run(args, text=True, capture_output=True, **kwargs)
    if result.returncode:
        raise ValueError(result.stderr.strip() or 'Command failed: ' + args[0])
    return result.stdout


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(value)
    temporary.chmod(0o600)
    os.replace(temporary, path)


def native_config(infos):
    return json.loads(command(['perl', str(Path(__file__).with_suffix('.pl'))],
                              input=json.dumps({'vmids': [i['vmid'] for i in infos]})))


def networks(info):
    project = 'bd-' + info['id']
    ids = command(['docker', 'network', 'ls', '-q', '--filter', 'label=com.docker.compose.project=' + project]).split()
    if not ids:
        return []
    items = json.loads(command(['docker', 'network', 'inspect', *ids]))
    result = []
    for item in items:
        if item['Driver'] != 'bridge' or item.get('EnableIPv6'):
            raise ValueError('DCT firewall requires owned IPv4 Docker bridge networks')
        bridge = item.get('Options', {}).get('com.docker.network.bridge.name') or 'br-' + item['Id'][:12]
        if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', bridge):
            raise ValueError('Invalid Docker bridge name')
        for identifier in item.get('Containers', {}):
            container = json.loads(command(['docker', 'inspect', identifier]))[0]
            if container['Config'].get('Labels', {}).get('com.docker.compose.project') != project:
                raise ValueError('DCT firewall cannot protect a network shared with another project')
        result.append(bridge)
    return sorted(set(result))


def check_options(options):
    for name in ('dhcp', 'macfilter', 'ipfilter', 'ips', 'radv', 'ndp'):
        if name in options:
            raise ValueError(f'{name} is not supported for the Docker IPv4 endpoint')


def published_ports(info):
    project = 'bd-' + info['id']
    ids = command(['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=' + project]).split()
    if not ids:
        return []
    if runtime.load().rootless:
        from dctlib import projects as worker
        import host
        config = json.loads((worker.ROOT / info['id'] / 'compose.runtime.json').read_text())
        result = []
        for container in json.loads(command(['docker', 'inspect', *ids])):
            service = container['Config']['Labels']['com.docker.compose.service']
            addresses = [n['IPAddress'] for n in container.get('NetworkSettings', {}).get('Networks', {}).values() if n.get('IPAddress')]
            if not addresses:
                continue
            target = host.valid_address(addresses[0])
            for port in config['services'][service].get('ports', []):
                if port.get('host_ip') != info['address']:
                    raise ValueError('Published port is not bound to the DCT endpoint')
                protocol = port.get('protocol', 'tcp')
                if protocol not in ('tcp', 'udp'):
                    raise ValueError('Rootless published ports support TCP and UDP')
                def expand(value):
                    fields = str(value).split('-')
                    start, end = int(fields[0]), int(fields[-1])
                    if len(fields) > 2 or not 1 <= start <= end <= 65535:
                        raise ValueError('Invalid published port range')
                    return list(range(start, end + 1))
                published, targets = expand(port['published']), expand(port['target'])
                if len(published) != len(targets):
                    raise ValueError('Rootless port ranges must have equal lengths')
                result.extend((protocol, public, target, internal) for public, internal in zip(published, targets))
        return sorted(result)
    result = []
    for container in json.loads(command(['docker', 'inspect', *ids])):
        addresses = [n['IPAddress'] for name, n in container['NetworkSettings']['Networks'].items()
                     if name.startswith(project + '_') and n.get('IPAddress')]
        if not addresses:
            continue
        target = str(ipaddress.IPv4Address(addresses[0]))
        if runtime.load().rootless:
            import host
            target = host.valid_address(target)
        for port, mappings in (container['NetworkSettings'].get('Ports') or {}).items():
            target_port, protocol = port.split('/')
            if protocol not in ('tcp', 'udp', 'sctp'):
                raise ValueError('Unsupported published-port protocol')
            for mapping in mappings or []:
                if mapping['HostIp'] != info['address']:
                    raise ValueError('Published port is not bound to the DCT endpoint')
                host_port = int(mapping['HostPort'])
                if not 1 <= host_port <= 65535 or not 1 <= int(target_port) <= 65535:
                    raise ValueError('Invalid published port')
                result.append((protocol, host_port, target, int(target_port)))
    return sorted(result)


def set_members(lines):
    """Expand ipset nomatch entries using longest-prefix precedence."""
    entries = []
    for line in lines:
        words = shlex.split(line)
        if words[0] != 'add':
            continue
        network = ipaddress.ip_network(words[2], strict=False)
        if network.version == 4:
            entries.append((network, 'nomatch' in words[3:]))
    result = []
    for network, excluded in sorted(entries, key=lambda e: e[0].prefixlen):
        if excluded:
            remaining = []
            for existing in result:
                if existing.subnet_of(network):
                    continue
                remaining.extend(existing.address_exclude(network) if network.subnet_of(existing) else [existing])
            result = remaining
        else:
            result = list(ipaddress.collapse_addresses([*result, network]))
    return [str(n) for n in result]


def translate(line, names, original=False):
    words = shlex.split(line)
    substitutions = []
    while '--match-set' in words:
        index = words.index('--match-set')
        name, direction = words[index + 1:index + 3]
        if name not in names or direction not in ('src', 'dst'):
            raise ValueError('Unsupported native IPSet reference')
        start = index - 2
        negate = False
        if words[index - 1] == '!':
            start -= 1
            negate = True
        if words[start:start + 2] != ['-m', 'set']:
            raise ValueError('Unsupported native IPSet expression')
        placeholder = '198.18.0.' + str(len(substitutions) + 1)
        words[start:index + 3] = (['!'] if negate else []) + ['-s' if direction == 'src' else '-d', placeholder]
        substitutions.append((placeholder, '@' + names[name]))
    output = command(['iptables-translate', *words]).strip()
    if output.startswith("nft '"):
        output = 'nft ' + shlex.split(output)[1]
    prefix = 'nft add rule ip filter ' + words[1] + ' '
    if not output.startswith(prefix) or '# ' in output:
        raise ValueError('Native firewall expression has no supported nftables translation: ' + line)
    expression = output[len(prefix):]
    for placeholder, replacement in substitutions:
        expression = expression.replace(placeholder, replacement)
    if original:
        expression = expression.replace('ip daddr', 'ct original ip daddr')
        expression = re.sub(r'\b(tcp|udp|sctp) dport\b', r'meta l4proto \1 ct original proto-dst', expression)
    return expression


def render(infos, config, bridges, bindings=None, routed=None, inner=False):
    lines = ['table inet ' + TABLE + ' {']
    policies = {g['vmid']: g for g in config['guests']}
    lograte = config.get('log_ratelimit', {})
    limiter = ('limit rate ' + lograte.get('rate', '1/second') + ' burst ' + str(lograte.get('burst', 5)) + ' packets ') if lograte.get('enable', 1) else ''
    protected = []
    for info in infos:
        guest = policies[info['vmid']]
        if not (config['cluster_enabled'] and guest['options'].get('enable') and info.get('firewall')):
            continue
        check_options(guest['options'])
        protected.append(info)
        names = {name: 's' + hashlib.sha256(name.encode()).hexdigest()[:20] for name in guest['sets']}
        for name, entries in guest['sets'].items():
            if 'family inet6' in entries[0]:
                continue
            members = set_members(entries)
            # The same cluster set can occur in several guests.
            declaration = 'set ' + names[name] + ' {'
            if any(line.startswith(declaration) for line in lines):
                continue
            lines.append(declaration + ' type ipv4_addr; flags interval; auto-merge; ' +
                         ('elements = { ' + ', '.join(members) + ' }; ' if members else '') + '}')
        for direction, original in [('in', False), ('in', True), ('out', False)]:
            chain = 'd' + str(info['vmid']) + direction + ('nat' if original else '')
            lines.append('chain ' + chain + ' {')
            lines.append('ct state invalid drop')
            lines.append('ct state established,related accept')
            for rule in guest['rules'][direction]:
                expression = translate(rule, names, original)
                if ' log ' in expression or expression.startswith('log '):
                    expression = re.sub(r'limit rate \S+(?: burst \d+ packets)? ?', lambda _: limiter, expression)
                lines.append(expression)
            policy = guest['options'].get('policy_' + direction, 'DROP' if direction == 'in' else 'ACCEPT').lower()
            if policy not in ('drop', 'accept', 'reject'):
                raise ValueError('Unsupported guest firewall policy')
            level = guest['options'].get('log_level_' + direction, 'nolog')
            if level != 'nolog':
                severity = {'emerg': 0, 'alert': 1, 'crit': 2, 'err': 3, 'warning': 4, 'notice': 5, 'info': 6, 'debug': 7}[level]
                prefix = f':{info["vmid"]}:{severity}:{chain}: {policy.upper()}: '
                lines.append(limiter + 'log prefix ' + json.dumps(prefix) + ' group 0')
            lines.extend([policy, '}'])
    # Separate OUT and IN base chains: ACCEPT in the source policy must not
    # bypass the destination policy for cross-DCT traffic.
    for hook, direction, priority in [('input', 'out', -10), ('forward', 'out', -10),
                                      ('input', 'in', 0), ('forward', 'in', 0), ('output', 'in', 0)]:
        lines.append(f'chain {hook}_{direction} {{ type filter hook {hook} priority {priority}; policy accept;')
        if inner and hook == 'forward' and direction == 'in':
            # Host policy already checked traffic arriving through the owned uplink.
            lines.append('iifname "bd-uplink" meta nfproto ipv6 drop')
            lines.append('iifname "bd-uplink" return')
        if hook == 'forward':
            for info in protected:
                owned = bridges.get(info['id'], [])
                if owned:
                    interfaces = '{ ' + ', '.join(json.dumps(b) for b in owned) + ' }'
                    lines.append(f'iifname {interfaces} oifname {interfaces} return')
        for info in protected:
            chain = 'd' + str(info['vmid']) + direction
            address = str(ipaddress.IPv4Address(info['address'])) if info.get('address') else None
            if routed and hook == 'forward':
                addresses = routed.get(info['id'], [])
                if direction == 'in' and address:
                    lines.append(f'ct original ip daddr {address} jump {chain}nat')
                if addresses:
                    expression = '{ ' + ', '.join(addresses) + ' }'
                    lines.append(f'ip {"saddr" if direction == "out" else "daddr"} {expression} jump {chain}')
            if hook == 'output' and direction == 'in' and address:
                # OUTPUT DNAT can reroute after this hook; match the original
                # endpoint even when oifname still describes the local route.
                lines.append(f'ct original ip daddr {address} jump {chain}nat')
            for bridge in bridges.get(info['id'], []):
                interface = json.dumps(bridge)
                if direction == 'out':
                    lines.append(f'iifname {interface} meta nfproto ipv6 drop')
                    lines.append(f'iifname {interface} jump {chain}')
                elif hook != 'input':
                    lines.append(f'oifname {interface} meta nfproto ipv6 drop')
                    if address:
                        lines.append(f'oifname {interface} ct original ip daddr {address} jump {chain}nat')
                    lines.append(f'oifname {interface} jump {chain}')
            if direction == 'in' and hook in ('input', 'output') and address:
                lines.append(f'ip daddr {address} jump {chain}')
        lines.append('}')
    lines.append('}')
    # Force published connections through the routed path even for cross-bridge
    # clients. Otherwise docker-proxy opens a second host connection and loses
    # the original client identity before the guest's ingress policy.
    for hook in ('prerouting', 'output'):
        lines.insert(-1, f'chain published_{hook} {{ type nat hook {hook} priority -101; policy accept;')
        for info in protected:
            address = str(ipaddress.IPv4Address(info['address'])) if info.get('address') else None
            for protocol, port, target, target_port in (bindings or {}).get(info['id'], []):
                lines.insert(-1, f'ip daddr {address} {protocol} dport {port} dnat ip to {target}:{target_port}')
        lines.insert(-1, '}')
    return '\n'.join(lines) + '\n'


@contextlib.contextmanager
def lock():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / 'lock').open('w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def apply_rules(rules, table=TABLE, cache='applied.nft'):
    exists = subprocess.run(['nft', 'list', 'table', 'inet', table], capture_output=True).returncode == 0
    transaction = ('delete table inet ' + table + '\n' if exists else '') + rules
    command(['nft', '--check', '-f', '-'], input=transaction)
    pending = STATE / ('pending-' + cache)
    save(pending, rules)
    command(['nft', '-f', '-'], input=transaction)
    os.replace(pending, STATE / cache)


def reconcile():
    from dctlib import projects as worker
    rootless = runtime.load().rootless
    if rootless:
        import host
        # Cached rules must be able to restore while the daemon is starting.
        # Do not hold the firewall writer lock while waiting for its namespace.
        host.ready()
    with lock():
        if not worker.METADATA.is_dir():
            raise ValueError('DCT cluster configuration is unavailable; retaining firewall rules')
        metadata = [json.loads(path.read_text()) for path in worker.METADATA.glob('*.json')]
        infos = [i for i in metadata if i['node'] == worker.current_node() and i.get('vmid')]
        config = native_config(infos)
        bridges = {i['id']: networks(i) for i in infos if i.get('firewall') or rootless}
        bindings = {i['id']: published_ports(i) for i in infos if i.get('firewall') or rootless}
        if rootless:
            reconcile_rootless(infos, config, bridges, bindings)
            return
        rules = render(infos, config, bridges, bindings)
        previous = STATE / 'applied.nft'
        if not previous.exists() or previous.read_text() != rules or subprocess.run(
                ['nft', 'list', 'table', 'inet', TABLE], capture_output=True).returncode:
            apply_rules(rules)
        active = [i['vmid'] for i in infos if config['cluster_enabled'] and i.get('firewall') and
                  next(g for g in config['guests'] if g['vmid'] == i['vmid'])['options'].get('enable')]
        save(STATE / 'status.json', json.dumps({'updated': time.time(), 'error': None, 'active': active}))


def reconcile_rootless(infos, config, bridges, bindings):
    import host
    routed = {}
    for info in infos:
        ids = command(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=bd-' + info['id']]).split()
        values = json.loads(command(['docker', 'inspect', *ids])) if ids else []
        routed[info['id']] = sorted({host.valid_address(n['IPAddress']) for c in values
                                   for n in c.get('NetworkSettings', {}).get('Networks', {}).values() if n.get('IPAddress')})
    rules = render(infos, config, {}, bindings, routed=routed)
    inner = render(infos, config, bridges, inner=True)
    with host.namespace() as (_, _, prefix, descriptors):
        exists = subprocess.run(prefix + ['nft', 'list', 'table', 'inet', TABLE], pass_fds=descriptors, capture_output=True).returncode == 0
        transaction = ('delete table inet ' + TABLE + '\n' if exists else '') + inner
        command(prefix + ['nft', '--check', '-f', '-'], pass_fds=descriptors, input=transaction)
        command(prefix + ['nft', '-f', '-'], pass_fds=descriptors, input=transaction)
    apply_rules(rules)
    lines = ['table inet betterdocker_rootless {',
             'chain guard { type filter hook forward priority -20; policy accept;',
             'iifname "bd-rootless" meta nfproto ipv6 drop',
             'oifname "bd-rootless" meta nfproto ipv6 drop',
             'iifname "bd-rootless" ip saddr != { 10.240.0.0/16, 169.254.240.2 } drop',
             'oifname "bd-rootless" ct state established,related accept',
             'oifname "bd-rootless" ct status dnat accept']
    for entries in bindings.values():
        for protocol, _, target, port in entries:
            lines.append(f'oifname "bd-rootless" ip daddr {target} {protocol} dport {port} accept')
    lines.extend(['oifname "bd-rootless" ip daddr 10.240.0.0/16 drop', '}',
             'chain outbound { type nat hook postrouting priority 101; policy accept;',
             # A published cross-DCT connection must return through host conntrack.
             # Otherwise the inner bridges return directly with the untranslated IP.
             'iifname "bd-rootless" oifname "bd-rootless" ct status dnat masquerade',
             'ip saddr { 10.240.0.0/16, 169.254.240.2 } oifname != "bd-rootless" masquerade', '}'])
    for hook in ('prerouting', 'output'):
        lines.append(f'chain published_{hook} {{ type nat hook {hook} priority -102; policy accept;')
        for info in infos:
            address = str(ipaddress.IPv4Address(info['address'])) if info.get('address') else None
            for protocol, port, target, target_port in bindings.get(info['id'], []):
                lines.append(f'ip daddr {address} {protocol} dport {port} dnat ip to {target}:{target_port}')
        lines.append('}')
    lines.append('}')
    exists = subprocess.run(['nft', 'list', 'table', 'inet', 'betterdocker_rootless'], capture_output=True).returncode == 0
    transaction = ('delete table inet betterdocker_rootless\n' if exists else '') + '\n'.join(lines) + '\n'
    command(['nft', '--check', '-f', '-'], input=transaction)
    command(['nft', '-f', '-'], input=transaction)
    save(STATE / 'rootless.nft', '\n'.join(lines) + '\n')
    active = [i['vmid'] for i in infos if config['cluster_enabled'] and i.get('firewall') and
              next(g for g in config['guests'] if g['vmid'] == i['vmid'])['options'].get('enable')]
    save(STATE / 'status.json', json.dumps({'updated': time.time(), 'error': None, 'active': active, 'runtime': 'rootless'}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['apply', 'restore', 'watch'])
    args = parser.parse_args()
    if args.action == 'restore':
        with lock():
            saved = STATE / 'applied.nft'
            if saved.exists():
                apply_rules(saved.read_text())
            saved = STATE / 'rootless.nft'
            if runtime.load().rootless and saved.exists():
                apply_rules(saved.read_text(), table='betterdocker_rootless', cache='rootless.nft')
        return
    while True:
        try:
            reconcile()
        except Exception as error:
            save(STATE / 'status.json', json.dumps({'updated': time.time(), 'error': str(error)}))
            if args.action != 'watch':
                print(str(error), file=sys.stderr)
                raise SystemExit(1)
            print(str(error), flush=True)
        if args.action != 'watch':
            return
        time.sleep(2)


if __name__ == '__main__':
    main()
