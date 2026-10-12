"""Allow endpoint NDP and DHCPv6 through PVE's separate host policy."""
import json
import subprocess

TAG = 'betterdocker-control6'


def ensure(endpoint, remove=False):
    import firewall
    interface = endpoint['interface']
    marker = TAG + ':' + interface
    native = subprocess.run(['nft', '-j', 'list', 'table', 'inet', 'proxmox-firewall'], capture_output=True, text=True)
    if native.returncode == 0:
        state = json.loads(native.stdout)['nftables']
        changes = []
        for direction in ('input', 'output'):
            chain = TAG + '-' + direction
            chains = [row['chain']['name'] for row in state if 'chain' in row]
            rules = [row['rule'] for row in state if 'rule' in row]
            current = [rule for rule in rules if rule['chain'] == chain]
            if any(not rule.get('comment', '').startswith(TAG + ':') for rule in current):
                raise ValueError('IPv6 endpoint control chain belongs to another administrator')
            owned = [rule for rule in current if rule.get('comment') == marker]
            if remove:
                changes.extend('delete rule inet proxmox-firewall ' + chain + ' handle ' + str(rule['handle']) for rule in owned)
                continue
            if chain not in chains:
                changes.append('add chain inet proxmox-firewall ' + chain)
            if len(owned) != 2:
                changes.extend('delete rule inet proxmox-firewall ' + chain + ' handle ' + str(rule['handle']) for rule in owned)
                selector = ('iifname' if direction == 'input' else 'oifname') + ' ' + json.dumps(interface)
                changes.append('add rule inet proxmox-firewall ' + chain + ' ' + selector + ' meta nfproto ipv6 ip6 hoplimit 255 icmpv6 type { 133, 134, 135, 136 } accept comment ' + json.dumps(marker))
                ports = 'udp sport 547 udp dport 546' if direction == 'input' else 'udp sport 546 udp dport 547'
                changes.append('add rule inet proxmox-firewall ' + chain + ' ' + selector + ' meta nfproto ipv6 ' + ports + ' accept comment ' + json.dumps(marker))
            hooks = [rule for rule in rules if rule['chain'] == direction and rule.get('comment') == TAG]
            if not hooks:
                changes.append('insert rule inet proxmox-firewall ' + direction + ' jump ' + chain + ' comment ' + json.dumps(TAG))
        if changes:
            content = '\n'.join(changes) + '\n'
            firewall.command(['nft', '--check', '-f', '-'], input=content)
            firewall.command(['nft', '-f', '-'], input=content)
        return
    chain = 'BD6-CONTROL'
    existing = subprocess.run(['ip6tables', '-w', '-S', chain], capture_output=True, text=True)
    changes = ['*filter']
    if existing.returncode:
        if remove:
            return
        changes.append(':' + chain + ' - [0:0]')
    rules = existing.stdout.splitlines()[1:] if existing.returncode == 0 else []
    owned = [rule for rule in rules if marker in rule]
    if remove:
        changes.extend(rule.replace('-A ', '-D ', 1) for rule in owned)
    else:
        wanted = []
        for direction, sport, dport in (('-i', 547, 546), ('-o', 546, 547)):
            for kind in (133, 134, 135, 136):
                wanted.append(f'-A {chain} {direction} {interface} -p ipv6-icmp -m hl --hl-eq 255 --icmpv6-type {kind} -m comment --comment {marker} -j ACCEPT')
            wanted.append(f'-A {chain} {direction} {interface} -p udp --sport {sport} --dport {dport} -m comment --comment {marker} -j ACCEPT')
        # iptables -S can add quotes and explicit protocol match modules.
        for rule in wanted:
            probe = subprocess.run(['ip6tables', '-w', '-C', *rule.split()[1:]], capture_output=True)
            if probe.returncode:
                changes.append(rule)
        for hook in ('INPUT', 'OUTPUT'):
            probe = subprocess.run(['ip6tables', '-w', '-C', hook, '-j', chain], capture_output=True)
            if probe.returncode:
                changes.append('-I ' + hook + ' 1 -j ' + chain)
    if len(changes) > 1:
        firewall.command(['ip6tables-restore', '--wait', '--noflush'], input='\n'.join(changes + ['COMMIT']) + '\n')
