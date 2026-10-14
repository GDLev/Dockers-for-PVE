"""Apply owned firewall tables, preserving a working generation on reload failure."""
import hashlib
import json
import subprocess


def generation(rules):
    return 'generation_' + hashlib.sha256(rules.encode()).hexdigest()[:24]


def stamped(rules):
    head, end = rules.rsplit('}', 1)
    return head + 'chain ' + generation(rules) + ' { }\n}' + end


def present(table, rules, prefix=(), descriptors=()):
    result = subprocess.run(list(prefix) + ['nft', '-j', 'list', 'table', 'inet', table], pass_fds=descriptors, capture_output=True, text=True)
    if result.returncode: return False
    entries = json.loads(result.stdout)['nftables']
    return any(entry.get('chain', {}).get('name') == generation(rules) for entry in entries)


def closed_rootless():
    import firewall
    result = subprocess.run(['nft', 'list', 'table', 'inet', 'betterdocker_rootless'], capture_output=True)
    rules = 'table inet betterdocker_rootless {\nchain guard {\ntype filter hook forward priority -20; policy accept;\niifname "bd-rootless" drop\noifname "bd-rootless" drop\n}\n}\n'
    firewall.command(['nft', '-f', '-'], input=('delete table inet betterdocker_rootless\n' if not result.returncode else '') + rules)


def transaction(plans, activate=None, rootless=False):
    import firewall
    saved = []
    changed = False
    caches = {}
    for table, rules, cache, prefix, descriptors in plans:
        path = firewall.STATE / cache
        caches[cache] = path.read_text() if path.exists() else None
        changed |= not path.exists() or path.read_text() != rules or not present(table, rules, prefix, descriptors)
        old = subprocess.run(list(prefix) + ['nft', 'list', 'table', 'inet', table], pass_fds=descriptors, capture_output=True, text=True)
        saved.append((table, old.stdout if not old.returncode else None, prefix, descriptors))
    if not changed:
        if activate: activate()
        return
    groups = {}
    for (table, rules, cache, prefix, descriptors), (_, old, _, _) in zip(plans, saved):
        key = (tuple(prefix), tuple(descriptors))
        groups.setdefault(key, []).append(('delete table inet ' + table + '\n' if old is not None else '') + stamped(rules))
    # Validate every namespace before changing any of them.
    for (prefix, descriptors), parts in groups.items():
        firewall.command(list(prefix) + ['nft', '--check', '-f', '-'], pass_fds=descriptors, input='\n'.join(parts))
    modified = False
    try:
        if rootless:
            closed_rootless()
            modified = True
        for (prefix, descriptors), parts in groups.items():
            firewall.command(list(prefix) + ['nft', '-f', '-'], pass_fds=descriptors, input='\n'.join(parts))
            modified = True
        if activate: activate()
        for table, rules, cache, prefix, descriptors in plans:
            if not present(table, rules, prefix, descriptors):
                raise ValueError('Required firewall generation is missing')
        for table, rules, cache, prefix, descriptors in plans:
            firewall.save(firewall.STATE / cache, rules)
    except BaseException:
        if modified:
            try:
                restore = {}
                for table, old, prefix, descriptors in saved:
                    current = subprocess.run(list(prefix) + ['nft', 'list', 'table', 'inet', table], pass_fds=descriptors, capture_output=True)
                    part = ('delete table inet ' + table + '\n' if not current.returncode else '') + (old or '')
                    restore.setdefault((tuple(prefix),tuple(descriptors)), []).append(part)
                for (prefix, descriptors), parts in restore.items():
                    firewall.command(list(prefix) + ['nft', '-f', '-'], pass_fds=descriptors, input='\n'.join(parts))
                for cache, previous in caches.items():
                    path = firewall.STATE / cache
                    if previous is None: path.unlink(missing_ok=True)
                    else: firewall.save(path, previous)
            except BaseException:
                if rootless: closed_rootless()
                raise ValueError('Firewall reload and rollback failed' + ('; rootless forwarding remains closed' if rootless else ''))
        raise
