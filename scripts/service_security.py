"""Docker service security settings and host AppArmor availability."""
from pathlib import Path
import re

KEYS = {'user', 'read_only', 'cap_drop', 'no_new_privileges', 'apparmor'}
CAPABILITIES = set(('ALL CHOWN DAC_OVERRIDE DAC_READ_SEARCH FOWNER FSETID KILL SETGID SETUID SETPCAP '
    'LINUX_IMMUTABLE NET_BIND_SERVICE NET_BROADCAST NET_ADMIN NET_RAW IPC_LOCK IPC_OWNER SYS_MODULE '
    'SYS_RAWIO SYS_CHROOT SYS_PTRACE SYS_PACCT SYS_ADMIN SYS_BOOT SYS_NICE SYS_RESOURCE SYS_TIME '
    'SYS_TTY_CONFIG MKNOD LEASE AUDIT_WRITE AUDIT_CONTROL SETFCAP MAC_OVERRIDE MAC_ADMIN SYSLOG '
    'WAKE_ALARM BLOCK_SUSPEND AUDIT_READ PERFMON BPF CHECKPOINT_RESTORE').split())


def validate(values):
    result = {}
    for key in KEYS & values.keys():
        value = values[key]
        if key == 'user':
            if not isinstance(value, str) or value and not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}(?::[a-zA-Z0-9_.-]{1,64})?', value):
                raise ValueError('Invalid container user; use a name or UID[:GID]')
        elif key in ('read_only', 'no_new_privileges'):
            if type(value) is not bool:
                raise ValueError(key + ' must be a boolean')
        elif key == 'cap_drop':
            if not isinstance(value, list) or len(value) > len(CAPABILITIES) or any(not isinstance(cap, str) or cap not in CAPABILITIES for cap in value):
                raise ValueError('Invalid dropped capabilities')
            value = sorted(set(value))
        elif not isinstance(value, str) or value and not re.fullmatch(r'[a-zA-Z0-9_./-]{1,128}', value):
            raise ValueError('Invalid AppArmor profile')
        result[key] = value
    return result


def options(service):
    value = service.get('security_opt') or []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError('Invalid security options')
    return value


def values(service):
    opts = options(service)
    return {'user': service.get('user', ''), 'read_only': service.get('read_only', False),
        'cap_drop': service.get('cap_drop', []),
        'no_new_privileges': any(o in ('no-new-privileges:true', 'no-new-privileges=true', 'no-new-privileges') for o in opts),
        'apparmor': next((o.split('=', 1)[1] if '=' in o else o.split(':', 1)[1] for o in opts if o.startswith(('apparmor=', 'apparmor:'))), '')}


def apply(service, settings):
    for key in ('user', 'read_only', 'cap_drop'):
        if key in settings:
            if settings[key]:
                service[key] = settings[key]
            else:
                service.pop(key, None)
    opts = options(service)
    for key, prefix in (('no_new_privileges', 'no-new-privileges'), ('apparmor', 'apparmor')):
        if key in settings:
            opts = [o for o in opts if not (o == prefix or o.startswith((prefix + ':', prefix + '=')))]
            if settings[key]:
                opts.append(prefix + ':' + ('true' if key == 'no_new_privileges' else settings[key]))
    if opts:
        service['security_opt'] = opts
    else:
        service.pop('security_opt', None)


def availability(selected):
    if selected.rootless:
        return {'supported': False, 'profiles': []}
    enabled = Path('/sys/module/apparmor/parameters/enabled')
    supported = enabled.exists() and enabled.read_text().strip() == 'Y'
    profiles = Path('/sys/kernel/security/apparmor/profiles')
    loaded = [line.rsplit(' (', 1)[0] for line in profiles.read_text().splitlines() if line.endswith(' (enforce)')] if supported and profiles.exists() else []
    loaded = [name for name in loaded if re.fullmatch(r'[a-zA-Z0-9_./-]{1,128}', name)]
    return {'supported': supported, 'profiles': sorted(set(['docker-default', *loaded])) if supported else []}


def check(config, selected):
    profiles = None
    for service in config.get('services', {}).values():
        profile = values(service)['apparmor']
        if not profile:
            continue
        if selected.rootless:
            raise ValueError('Container AppArmor profiles require rootful Docker')
        if profile == 'unconfined':
            continue
        profiles = profiles or availability(selected)
        if not profiles['supported'] or profile not in profiles['profiles']:
            raise ValueError('AppArmor profile is unavailable on this node: ' + profile)


def delegated(opts):
    # Only protections that narrow privileges are available to guest administrators.
    if any(o not in ('no-new-privileges', 'no-new-privileges:true', 'no-new-privileges=true',
                     'apparmor:docker-default', 'apparmor=docker-default') for o in opts):
        raise ValueError('Custom or disabled security profiles require node administrator access')
