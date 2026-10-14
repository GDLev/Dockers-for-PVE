"""Administrator-owned workload policy, shared by deployment and preserved restores."""
import copy
import json
from pathlib import Path
import service_security

FILE = 'security-policy.json'
PROFILE = 'betterdocker-default'
CAPS = frozenset('CHOWN DAC_OVERRIDE FOWNER FSETID KILL SETGID SETUID SETPCAP NET_BIND_SERVICE SETFCAP AUDIT_WRITE'.split())
MODES = ('standard', 'hardened')


def capabilities(value):
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ValueError('Capabilities must be a list of names')
    return {v.upper().removeprefix('CAP_') for v in value}


def mode(directory, creating=False):
    from . import projects as w
    path = Path(directory) / FILE
    if path.exists():
        from .secure_io import read_json
        value = read_json(path)
        if not isinstance(value, dict) or set(value) != {'mode'} or value['mode'] not in MODES:
            raise ValueError('Invalid administrator security policy')
        return value['mode']
    return 'hardened' if creating and not w.trusted_management() else 'standard'


def available(selected):
    if selected.rootless:
        return
    profiles = Path('/sys/kernel/security/apparmor/profiles')
    if not profiles.exists() or PROFILE + ' (enforce)' not in profiles.read_text().splitlines():
        raise ValueError('Hardened DCT requires enforcing AppArmor profile ' + PROFILE)


def check_profile(identifier):
    from . import projects as w
    if mode(w.ROOT / identifier) == 'hardened':
        available(w.node_runtime.load())


def incompatible(config, selected):
    result = []
    for name, service in config.get('services', {}).items():
        for key in ('privileged', 'devices', 'device_cgroup_rules', 'volumes_from', 'sysctls', 'runtime'):
            if service.get(key):
                result.append(name + ': ' + key)
        for key in ('pid', 'ipc', 'uts', 'userns_mode', 'network_mode'):
            if service.get(key) not in (None, '', 'private', 'none'):
                result.append(name + ': ' + key)
        if capabilities(service.get('cap_add') or []) - CAPS:
            result.append(name + ': capabilities outside the hardened allowlist')
        for option in service_security.options(service):
            if option.startswith(('seccomp:', 'seccomp=')) or option in ('no-new-privileges:false', 'no-new-privileges=false'):
                result.append(name + ': ' + option.split('=', 1)[0].split(':', 1)[0])
            if option.startswith(('apparmor:', 'apparmor=')):
                profile = option.replace('=', ':', 1).split(':', 1)[1]
                if selected.rootless or profile not in ('docker-default', PROFILE):
                    result.append(name + ': unsupported AppArmor profile')
    return sorted(set(result))


def defaults(config):
    for service in config['services'].values():
        dropped = capabilities(service.get('cap_drop') or [])
        additions = capabilities(service['cap_add'] or []) if 'cap_add' in service else (set() if 'ALL' in dropped else set(CAPS))
        service.setdefault('labels', {})['betterdocker.policy-caps'] = json.dumps(sorted(additions - dropped))


def apply(config, selected, policy='hardened', strict=True, storage=None):
    if policy == 'standard':
        return config
    from . import filesystem_policy
    filesystem_policy.config(config, selected, policy, storage)
    issues = incompatible(config, selected)
    if strict and issues:
        raise ValueError('Incompatible hardened configuration: ' + '; '.join(issues))
    available(selected)
    for service in config['services'].values():
        dropped = capabilities(service.get('cap_drop') or [])
        # Once canonicalized, ALL is the mandatory baseline, not an instruction
        # to discard the explicitly selected, bounded additions.
        base = service.get('labels', {}).get('betterdocker.policy-caps')
        additions = capabilities(json.loads(base)) if base is not None else (capabilities(service['cap_add'] or []) if 'cap_add' in service else (set() if 'ALL' in dropped else set(CAPS)))
        if additions - CAPS:
            raise ValueError('Stored capability baseline exceeds hardened policy')
        service.setdefault('labels', {})['betterdocker.policy-caps'] = json.dumps(sorted(additions))
        service['cap_drop'] = sorted(dropped | {'ALL'})
        service['cap_add'] = sorted(additions - (dropped - {'ALL'}))
        opts = [o for o in service_security.options(service) if not o.startswith(('no-new-privileges', 'apparmor'))]
        opts.append('no-new-privileges:true')
        if not selected.rootless:
            opts.append('apparmor:' + PROFILE)
        service['security_opt'] = opts
    return config


def settings(directory, values):
    if mode(directory) != 'hardened':
        return
    from .filesystem_policy import required
    from . import projects as w
    if required(w.node_runtime.load(), 'hardened') and values.get('read_only') is False:
        raise ValueError('Mandatory read-only root filesystem cannot be disabled')
    if values.get('no_new_privileges') is False or ('cap_drop' in values and 'ALL' not in values['cap_drop']):
        raise ValueError('Mandatory hardened security settings cannot be removed')
    if 'apparmor' in values:
        from . import projects as w
        expected = '' if w.node_runtime.load().rootless else PROFILE
        if values['apparmor'] != expected:
            raise ValueError('Mandatory AppArmor profile cannot be changed')


def preserved(config, selected, policy, storage=None):
    """Adapt the managed profile of saved state to its destination runtime."""
    config = copy.deepcopy(config)
    if policy == 'hardened' and selected.rootless:
        for service in config['services'].values():
            service['security_opt'] = [option for option in service_security.options(service)
                                       if option not in ('apparmor:' + PROFILE, 'apparmor=' + PROFILE)]
    return apply(config, selected, policy, storage=storage)


def host(body, selected, policy, storage=None):
    if policy != 'hardened':
        return body
    from . import filesystem_policy
    filesystem_policy.host(body, selected, policy, storage)
    available(selected)
    host = body.setdefault('HostConfig', {})
    if host.get('Privileged') or host.get('Devices') or host.get('DeviceRequests') or host.get('DeviceCgroupRules') or host.get('Sysctls') or host.get('Runtime', 'runc') not in ('', 'runc'):
        raise ValueError('Preserved container is incompatible with hardened policy')
    for key in ('PidMode', 'IpcMode', 'UTSMode', 'UsernsMode', 'CgroupnsMode'):
        if host.get(key) not in (None, '', 'private'):
            raise ValueError('Preserved container shares a namespace')
    if host.get('NetworkMode') == 'host':
        raise ValueError('Hardened container cannot use host networking')
    dropped = capabilities(host.get('CapDrop') or [])
    additions = capabilities(host.get('CapAdd') or []) if 'ALL' in dropped else capabilities(host.get('CapAdd') or sorted(CAPS))
    if additions - CAPS:
        raise ValueError('Preserved container capabilities exceed hardened policy')
    host['CapAdd'] = sorted(additions - (dropped - {'ALL'}))
    host['CapDrop'] = sorted(dropped | {'ALL'})
    host['SecurityOpt'] = ['no-new-privileges'] + ([] if selected.rootless else ['apparmor=' + PROFILE])
    return body


def check_start(identifier):
    from . import projects as w
    directory = w.ROOT / identifier
    if mode(directory) != 'hardened':
        return
    available(w.node_runtime.load())
    ids = [row['id'] for row in w.container_records(identifier)]
    for row in json.loads(w.capture(['docker', 'inspect', *ids])) if ids else []:
        from .filesystem_policy import host as filesystem_host
        filesystem_host(copy.deepcopy(row), w.node_runtime.load(), 'hardened', live=True)
        actual = row['HostConfig']
        host(copy.deepcopy(row), w.node_runtime.load(), 'hardened')
        opts = actual.get('SecurityOpt') or []
        if 'ALL' not in capabilities(actual.get('CapDrop') or []) or capabilities(actual.get('CapAdd') or []) - CAPS or not any(o in ('no-new-privileges', 'no-new-privileges:true', 'no-new-privileges=true') for o in opts):
            raise ValueError('Container does not enforce its hardened policy; apply security policy before Start')
        if any(o.startswith(('seccomp:', 'seccomp=')) for o in opts) or (not w.node_runtime.load().rootless and row.get('AppArmorProfile') != PROFILE):
            raise ValueError('Container security profile does not match its mandatory policy')


def state(identifier, node):
    from . import projects as w
    from .secure_io import read_json
    info = w.detail(identifier, node)
    directory = w.ROOT / identifier
    config = read_json(directory / 'compose.runtime.json')
    issues = incompatible(config, w.node_runtime.load())
    try:
        prospective = copy.deepcopy(config)
        from .filesystem_policy import required
        if required(w.node_runtime.load(), 'hardened'):
            for service in prospective['services'].values():
                service['read_only'] = True
        candidate = apply(prospective, w.node_runtime.load())
        w.validate_delegated_config(config, identifier)
        from .service_recreate import settings_body, defaults_state
        rows = w.container_records(identifier)
        defaults = defaults_state(directory)
        for row in json.loads(w.capture(['docker', 'inspect', '--size', *[r['id'] for r in rows]])) if rows else []:
            name = row['Config']['Labels']['com.docker.compose.service']
            body = settings_body(row, config['services'][name], candidate['services'][name], defaults)
            host(body, w.node_runtime.load(), 'hardened')
    except ValueError as error:
        issues.append(str(error))
    records = w.container_records(identifier)
    return {'mode': mode(directory), 'capabilities': sorted(CAPS), 'incompatible': sorted(set(issues)),
            'digest': info.get('digest', ''), 'pending': bool(w.load_pending(directory) or w.load_pending_network(directory)),
            'running': any(row['state'] not in ('created', 'exited', 'dead', 'stopped') for row in records)}


def convert(identifier, node, policy, digest=None):
    from . import projects as w
    from .secure_io import read_json
    from .service_recreate import execute, settings_body, defaults_state
    if policy not in MODES:
        raise ValueError('Invalid security policy')
    with w.operation(identifier, 'security-policy'), w.locked(identifier):
        info = w.detail(identifier, node)
        w.check_protection(info)
        w.assert_configuration_digest(identifier, node, digest)
        directory = w.ROOT / identifier
        if w.load_pending(directory) or w.load_pending_network(directory):
            raise ValueError('Apply or revert pending changes before changing security policy')
        rows = w.container_records(identifier)
        w.require_stopped(rows)
        original = read_json(directory / 'compose.runtime.json')
        prospective = copy.deepcopy(original)
        from .filesystem_policy import required
        if required(w.node_runtime.load(), policy):
            for service in prospective['services'].values():
                service['read_only'] = True
        candidate = apply(prospective, w.node_runtime.load(), policy)
        if policy == 'hardened':
            w.validate_delegated_config(original, identifier)
        defaults = defaults_state(directory)
        entries = []
        for row in json.loads(w.capture(['docker', 'inspect', '--size', *[r['id'] for r in rows]])) if rows else []:
            name = row['Config']['Labels']['com.docker.compose.service']
            body = settings_body(row, original['services'][name], candidate['services'][name], defaults)
            host(body, w.node_runtime.load(), policy)
            entries.append({'source': row, 'body': body})
        metadata = {k: v for k, v in info.items() if k not in ('deployment', 'digest')}
        metadata['security_policy'] = policy
        saved_settings = copy.deepcopy(w.load_settings(directory))
        if policy == 'hardened':
            for name, values in saved_settings.items():
                if name in candidate['services']:
                    security = service_security.values(candidate['services'][name])
                    values.update({key: security[key] for key in ('read_only', 'cap_drop', 'no_new_privileges', 'apparmor')})
        execute(info, '*', entries, candidate, saved_settings, {}, defaults,
                updates={FILE: json.dumps({'mode': policy})}, metadata=metadata)
        w.refresh(node)
