#!/usr/bin/env python3
"""Operations behind the native PVE dct command handler."""
# Check the installed code tree before importing non-frozen modules.
import os as _boot_os
import sys as _boot_sys
if __file__.startswith('/usr/local/lib/betterdocker/'):
    _boot_root = '/usr/local/lib/betterdocker'
    for _boot_dir, _boot_dirs, _boot_files in _boot_os.walk(_boot_root, followlinks=False):
        for _boot_name in [_boot_dir, *(_boot_os.path.join(_boot_dir, n) for n in _boot_dirs + _boot_files)]:
            _boot_info = _boot_os.stat(_boot_name, follow_symlinks=False)
            if _boot_info.st_uid != 0 or _boot_info.st_mode & 0o022 or _boot_os.path.islink(_boot_name):
                _boot_sys.stderr.write('BetterDocker: unsafe installed helper ownership or permissions\n')
                raise SystemExit(1)
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

CONFIG_KEYS = ('description', 'tags', 'protection', 'onboot', 'startup', 'hookscript', 'delete')
RESOURCE_KEYS = {'cpulimit': 'cpus', 'cpuunits': 'cpuunits', 'memory': 'memory', 'swap': 'swap',
                 'pids': 'pids', 'nameserver': 'dns', 'searchdomain': 'dns_search', 'restart-policy': 'restart'}


def api(method, path, **params):
    command = ['pvesh', method, path, '--output-format', 'json']
    for key, value in params.items():
        if value is not None:
            command += ['--' + key, json.dumps(value) if isinstance(value, (dict, list)) else str(int(value)) if isinstance(value, bool) else str(value)]
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError('PVE API request failed')
    lines = result.stdout.splitlines()
    if not lines:
        return None
    # Local pvesh workers can write task logs before their JSON response.
    for index in range(len(lines) - 1, -1, -1):
        try:
            value = json.loads(lines[index])
        except json.JSONDecodeError:
            continue
        if index:
            print('\n'.join(lines[:index]))
        return value
    raise RuntimeError('PVE API returned no JSON response')


def guest(vmid):
    matches = [item for item in api('get', '/cluster/resources', type='vm')
               if item['type'] == 'docker' and int(item['vmid']) == vmid]
    if len(matches) != 1:
        raise ValueError(f'DCT {vmid} not found')
    item = matches[0]
    return '/nodes/' + item['node'] + '/docker/' + item['id'].split('/')[-1]


def task_path(upid):
    parts = upid.split(':')
    if len(parts) < 3 or parts[0] != 'UPID' or not parts[1]:
        raise ValueError('Expected a PVE task UPID')
    return '/nodes/' + parts[1] + '/tasks/' + upid


def wait_task(upid):
    path = task_path(upid)
    while True:
        result = api('get', path + '/status')
        if result['status'] != 'running':
            status = result.get('exitstatus', 'Task failed')
            if status != 'OK' and not re.fullmatch(r'WARNINGS: \d+', status):
                raise RuntimeError(status)
            if status != 'OK':
                print(status, file=sys.stderr)
            return result
        time.sleep(1)


def finish_task(result, params):
    if isinstance(result, str) and result.startswith('UPID:'):
        wait_task(result)


def net0(value):
    values = {}
    for part in value.split(','):
        key, separator, setting = part.partition('=')
        if not separator or key in values:
            raise ValueError('Use net0 key=value pairs without duplicate keys')
        values[key] = setting
    unknown = set(values) - {'name', 'bridge', 'ip', 'hwaddr', 'mtu', 'firewall', 'ip6', 'gw', 'gw6', 'tag', 'link_down', 'rate', 'ports'}
    if unknown:
        raise ValueError('Unsupported DCT net0 option: ' + ', '.join(sorted(unknown)))
    if not re.fullmatch(r'eth(?:[0-9]|1[0-5])', values.get('name', 'eth0')):
        raise ValueError('Use an interface name eth0 through eth15')
    result = {key: values[key] for key in ('bridge', 'hwaddr') if key in values}
    if 'ip' in values:
        result.update(ipv4mode=values['ip'] if values['ip'] in ('dhcp', 'none') else 'static',
                      cidr='' if values['ip'] in ('dhcp', 'none') else values['ip'])
    for key in ('gw', 'gw6', 'ports'):
        if key in values:
            result[key] = values[key].replace(';', ',') if key == 'ports' else values[key]
    if 'ip6' in values:
        result.update(ipv6mode=values['ip6'] if values['ip6'] in ('none', 'auto', 'dhcp') else 'static', cidr6='' if values['ip6'] in ('none', 'auto', 'dhcp') else values['ip6'])
    if 'rate' in values:
        result['rate'] = float(values['rate'])
    for key in ('mtu', 'firewall', 'tag', 'link_down'):
        if key in values:
            result[key] = int(values[key])
    return result


def format_net0(info):
    values = {'bridge': info['bridge'], 'ip': info['ipv4mode'] if info.get('ipv4mode') in ('dhcp', 'none') else info.get('cidr', info.get('address', ''))}
    values.update({key: info[key] for key in ('hwaddr', 'mtu', 'firewall', 'tag', 'link_down', 'rate', 'gw', 'gw6') if info.get(key)})
    if info.get('ipv6mode') in ('auto', 'dhcp'):
        values['ip6'] = info['ipv6mode']
    elif info.get('cidr6'):
        values['ip6'] = info['cidr6']
    if info.get('ports'):
        values['ports'] = info['ports'].replace(',', ';')
    return ','.join(f'{key}={value}' for key, value in values.items())


def output(value, params):
    if params.get('output-format', 'text') != 'text':
        print(json.dumps(value, indent=2 if params['output-format'] == 'json-pretty' else None))
        return
    if isinstance(value, dict):
        for key, item in sorted(value.items()):
            if item is not None:
                print(f'{key}: {json.dumps(item) if isinstance(item, (dict, list)) else item}')
    else:
        print(value)


def table(rows, columns):
    rendered = [[str(row.get(key, '')) for key, _ in columns] for row in rows]
    headers = [title for _, title in columns]
    widths = [max([len(headers[i]), *(len(row[i]) for row in rendered)]) for i in range(len(columns))]
    for row in [headers, *rendered]:
        print(' '.join(value.ljust(width) for value, width in zip(row, widths)).rstrip())


def service(info, name=None):
    services = info.get('services', [])
    matches = [entry for entry in services if not name or entry['name'] == name]
    if len(matches) != 1:
        raise ValueError('Choose a Compose service with --service' if not name else f'Service {name} not found')
    return matches[0]


def configuration(base, info, params):
    value = api('get', base + '/config')
    value.pop('digest', None)
    value['hostname'] = info['name']
    value['security-policy'] = info.get('security_policy', 'standard')
    network = api('get', base + '/network')
    if not params.get('current'):
        network.update(network.get('pending', {}))
    interfaces = network.get('interfaces', {'net0': network})
    for name, endpoint in interfaces.items():
        value[name] = format_net0(endpoint)
    services = [service(info, params['service'])] if params.get('service') else info.get('services', [])
    for entry in services:
        settings = dict(entry['settings'])
        if not params.get('current'):
            settings.update(entry.get('pending', {}))
        prefix = '' if len(services) == 1 else entry['name'] + '.'
        for native, key in RESOURCE_KEYS.items():
            item = settings.get(key)
            if isinstance(item, list):
                item = ' '.join(item)
            if item is not None:
                value[prefix + native] = item
    storage = info.get('storage', {})
    if storage.get('volid'):
        value['data-disk'] = storage['volid']
    return value


def create(params):
    source = Path(params['source'])
    if source.is_file():
        content = source.read_text()
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict) or 'mode' not in data:
            data = {'mode': 'compose', 'compose': content}
    else:
        if params['source'].endswith(('.yaml', '.yml', '.json')):
            raise ValueError('Deployment file does not exist')
        data = {'mode': 'image', 'image': params['source'], 'ports': params.get('ports', '').replace(',', '\n')}
    data.update(vmid=params['vmid'], name=params.get('hostname') or data.get('name') or f'dct-{params["vmid"]}')
    networks = {key: net0(params[key]) for key in params if re.fullmatch(r'net(?:[0-9]|1[0-5])', key)}
    if networks:
        from dctlib.endpoints import KEYS
        primary = networks.get('net0', {})
        data.update({key: value for key, value in primary.items() if key in ('bridge', 'ipv4mode', 'cidr', 'hwaddr', 'mtu', 'firewall')})
        if len(networks) > 1 or set(primary) - {'bridge', 'ipv4mode', 'cidr', 'hwaddr', 'mtu', 'firewall'}:
            data['interfaces'] = networks
    if params.get('env-file'):
        data['environment'] = Path(params['env-file']).read_text()
    if not data.get('bridge') or not (data.get('cidr') or data.get('ipv4mode') in ('dhcp', 'none')):
        raise ValueError('Specify --net0 bridge=<bridge>,ip=<CIDR|dhcp>')
    node = params.get('node') or os.uname().nodename.split('.')[0]
    return api('create', '/nodes/' + node + '/docker', deployment=data, pool=params.get('pool'), start=params.get('start', 0))


def set_options(base, info, params):
    changes = {key: params[key] for key in CONFIG_KEYS if key in params and key != 'delete'}
    deleted = params.get('delete', '').split(',')
    if any(name and name not in ('startup', 'hookscript') and not re.fullmatch(r'net(?:[0-9]|1[0-5])', name) for name in deleted):
        raise ValueError('Only startup, hookscript or network interfaces can be deleted')
    limits = {key: params[native] for native, key in RESOURCE_KEYS.items() if native in params}
    networks = {key: net0(params[key]) for key in params if re.fullmatch(r'net(?:[0-9]|1[0-5])', key)}
    disks = {key: params[key] for key in params if re.fullmatch(r'mp(?:[1-9]|1[0-5])', key)}
    network = bool(networks) or any(deleted)
    if not changes and not limits and not network and not disks:
        raise ValueError('No configuration options specified')
    chosen = service(info, params.get('service')) if limits else None
    if chosen:
        values = dict(chosen['settings'])
        values.update(chosen.get('pending', {}))
        for key in ('dns', 'dns_search'):
            if key in limits:
                limits[key] = limits[key].split()
        values.update(limits)
    # Re-read the digest between distinct API updates in a combined set command.
    if changes:
        finish_task(api('set', base + '/config', **changes, digest=info['digest']), params)
    for name, values in networks.items():
        digest = api('get', base + '/management')['digest']
        finish_task(api('set', base + '/network', net=name, **values, digest=digest), params)
    for name in deleted:
        if not name:
            continue
        if name in ('startup', 'hookscript'):
            finish_task(api('set', base + '/config', delete=name, digest=api('get', base + '/management')['digest']), params)
            continue
        finish_task(api('set', base + '/network', net=name, delete=1, digest=api('get', base + '/management')['digest']), params)
    for name, value in disks.items():
        if value == 'delete':
            values = {'detach': 1}
        else:
            values = dict(part.split('=', 1) for part in value.split(','))
            if set(values) - {'storage', 'size', 'mp', 'service', 'backup', 'ro'}:
                raise ValueError('Invalid mount point option')
            values = {'storage': values.get('storage'), 'size_mib': int(values.get('size', 1024)),
                      'target': values.get('mp', ''), 'service': values.get('service') or service(info, params.get('service'))['name'],
                      'backup': int(values.get('backup', 1)), 'read_only': int(values.get('ro', 0))}
        finish_task(api('set', base + '/disks/' + name, **values, digest=api('get', base + '/management')['digest']), params)
    if limits:
        digest = api('get', base + '/management')['digest']
        finish_task(api('set', base + '/settings', service=chosen['name'], settings=values, digest=digest), params)


def dispatch(command, params):
    # PVE validates numeric options, but Perl can encode their scalars as strings.
    if command == 'cleanup':
        if os.geteuid() != 0:
            raise ValueError('Host administrator required')
        result = subprocess.check_output(['/usr/local/lib/betterdocker/provision.py', 'retained-data', params['project-id'],
            str(int(params.get('destroy-owned-data', 0)))], text=True)
        print(result, end='')
        return
    params = dict(params)
    for key in ('vmid', 'newid', 'memory', 'swap', 'cpuunits', 'pids', 'timeout',
                'onboot', 'protection', 'restart', 'start', 'cluster', 'current', 'verbose', 'full', 'services', 'replicas', 'exempt'):
        if key in params:
            params[key] = int(params[key])
    if 'cpulimit' in params:
        params['cpulimit'] = float(params['cpulimit'])
    if command == 'list':
        rows = [entry for entry in api('get', '/cluster/resources', type='vm') if entry['type'] == 'docker'
                and (params.get('cluster') or entry['node'] == os.uname().nodename.split('.')[0])]
        rows.sort(key=lambda entry: int(entry['vmid']))
        if params.get('output-format', 'text') == 'text':
            columns = [('vmid', 'VMID'), ('status', 'Status'), ('lock', 'Lock'), ('name', 'Name')]
            if params.get('cluster'):
                columns.append(('node', 'Node'))
            table(rows, columns)
        else:
            output(rows, params)
        return
    if command.startswith('task-'):
        action = command[5:]
        base = task_path(params['upid'])
        if action == 'wait':
            output(wait_task(params['upid']), params)
        elif action == 'stop':
            api('delete', base)
        elif action == 'log':
            for line in api('get', base + '/log'):
                print(line['t'])
        else:
            output(api('get', base + '/status'), params)
        return
    if command == 'limits':
        path = guest(params['vmid'])
        keys = {'cpus', 'memory', 'pids', 'services', 'replicas', 'exempt'} & params.keys()
        if keys:
            old = api('get', path + '/limits')
            values = {key: params.get(key, old[key]) for key in ('cpus', 'memory', 'pids')}
            values['cpus'] = float(values['cpus'])
            values.update(memory=int(values['memory']), pids=int(values['pids']), digest=old['digest'])
            values.update({key: params[key] for key in ('services', 'replicas') if key in params})
            if 'exempt' in params:
                values['exempt'] = bool(params['exempt'])
            result = api('set', path + '/limits', **values)
        else:
            result = api('get', path + '/limits')
        print('\n'.join(f'{key}: {result[key]}' for key in ('cpus', 'memory', 'pids', 'services', 'replicas', 'exempt') if key in result))
        return
    if command == 'create':
        finish_task(create(params), params)
        return
    base = guest(params['vmid'])
    if command in ('start', 'shutdown', 'stop', 'reboot'):
        finish_task(api('create', base + '/action', action='force-stop' if command == 'stop' else command,
                        timeout=params.get('timeout', 60) if command in ('shutdown', 'reboot') else None), params)
        return
    if command == 'destroy':
        finish_task(api('delete', base, **{'destroy-owned-data': params.get('destroy-owned-data', 0)}), params)
        return
    if command == 'network':
        output(api('get', base + '/network'), params)
        return
    if command == 'listsnapshot':
        snapshots = api('get', base + '/snapshots')
        if params.get('output-format', 'text') != 'text':
            output(snapshots, params)
        else:
            table(snapshots, [('name', 'Name'), ('parent', 'Parent'), ('description', 'Description')])
        return
    if command in ('snapshot', 'delsnapshot', 'rollback'):
        finish_task(api('create', base + '/snapshot', action={'snapshot': 'create', 'delsnapshot': 'delete', 'rollback': 'restore'}[command],
                        snapname=params['snapname'], description=params.get('description')), params)
        return
    info = api('get', base + '/management')
    if command == 'status':
        if params.get('output-format', 'text') != 'text' or params.get('verbose'):
            output({key: value for key, value in info.items() if key in ('vmid', 'name', 'node', 'status', 'template', 'metrics')}, params)
        else:
            print('status: ' + info['status'])
    elif command == 'config':
        output(configuration(base, info, params), params)
    elif command == 'pending':
        services = [service(info, params['service'])] if params.get('service') else info.get('services', [])
        rows = []
        for entry in services:
            rows += [dict(row, service=entry['name']) for row in api('get', base + '/pending', service=entry['name'])]
        network = api('get', base + '/network')
        pending = network.get('pending', {})
        if 'interfaces' in pending:
            existing = network.get('interfaces', {'net0': network})
            for name in sorted(set(existing) | set(pending['interfaces'])):
                before, after = existing.get(name), pending['interfaces'].get(name)
                if before != after:
                    rows.append({'key': name, 'value': format_net0(before) if before else '',
                                 'pending': format_net0(after) if after else 'delete', 'service': ''})
        elif pending:
            rows.append({'key':'net0', 'value':format_net0(network), 'pending':format_net0(dict(network, **pending)), 'service':''})
        if params.get('output-format', 'text') == 'text':
            table(rows, [('service', 'Service'), ('key', 'Option'), ('value', 'Current'), ('pending', 'Pending')])
        else:
            output(rows, params)
    elif command == 'set':
        if 'security-policy' in params:
            finish_task(api('set', base + '/security-policy', mode=params['security-policy'], digest=info['digest']), params)
            params = dict(params); params.pop('security-policy')
            info = api('get', base + '/management')
        set_options(base, info, params)
    elif command == 'recover':
        finish_task(api('create', base + '/recover'), params)
    elif command == 'migrate':
        finish_task(api('create', base + '/migrate', target=params['target'], storage=params.get('target-storage'), restart=params.get('restart'), timeout=params.get('timeout'), digest=info['digest']), params)
    elif command == 'clone':
        destination = net0(params['net0'])
        if 'firewall' in destination:
            raise ValueError('Clones inherit firewall settings; omit firewall from --net0')
        if not destination.get('cidr') and destination.get('ipv4mode') not in ('dhcp', 'none'):
            raise ValueError('Specify an independent address, DHCP or no IPv4 in --net0')
        destination['name'] = params.get('hostname') or info['name'][:55] + '-clone'
        finish_task(api('create', base + '/clone', newid=params['newid'], deployment=destination, pool=params.get('pool'),
                        storage=params.get('storage'), full=params.get('full', 1), digest=info['digest']), params)
    elif command == 'template':
        finish_task(api('create', base + '/template', digest=info['digest']), params)
    else:
        raise ValueError('Unknown command: ' + command)


def main():
    if len(sys.argv) != 2:
        raise ValueError('Use /usr/sbin/dct help for command syntax')
    dispatch(sys.argv[1], json.load(sys.stdin))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Stopped waiting. The PVE task continues; use dct task stop UPID to cancel it.', file=sys.stderr)
        sys.exit(130)
    except (ValueError, OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
