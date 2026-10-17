"""Internal command dispatch for PVE and host services."""
import argparse
import json
from pathlib import Path
from . import projects


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['cleanup-temporary', 'security-policy', 'set-security-policy', 'retained-data', 'recover', 'dhcp6-client', 'dhcp6-release', 'disk', 'image-state', 'image-preview', 'image-apply', 'storage-volume-state', 'delete-storage-volume', 'project-limits', 'network', 'network-config', 'firewall-check', 'daemon-unmanage', 'lifecycle', 'daemon-prepare', 'daemon-resume', 'lifecycle-stop', 'validate', 'create', 'detail', 'config', 'access-state', 'protection', 'check-digest', 'action', 'logs', 'restore', 'refresh', 'update', 'dhcp-client', 'dhcp-release', 'management', 'settings', 'pending', 'revert-settings', 'console-check', 'console', 'delegated-check', 'new-id', 'notes', 'rrddata', 'reserve-id', 'release-id', 'preview', 'tags', 'service-action', 'reset-settings', 'diagnostics', 'storage-limit', 'storage-config', 'purge-retained-storage', 'snapshots', 'snapshot', 'snapshot-config', 'snapshot-edit'])
    parser.add_argument('arguments', nargs='*')
    args = parser.parse_args()
    values = args.arguments
    if args.operation == 'retained-data':
        from .data import retained_data
        print(json.dumps(retained_data(values[0], destroy=len(values) > 1 and values[1] == '1')))
    elif args.operation == 'recover':
        from .recovery import recover
        recover(*values)
    elif args.operation in ('dhcp6-client', 'dhcp6-release'):
        from .ipv6_dynamic import client
        client(values[0], release=args.operation == 'dhcp6-release')
    elif args.operation == 'disk':
        from .disks import configure
        configure(values[0], values[1], values[2], json.loads(values[3]), values[4] if len(values) > 4 else None)
    elif args.operation == 'image-state':
        projects.detail(*values)
        from .images import state
        print(json.dumps(state(projects.ROOT / values[0])))
    elif args.operation in ('image-preview', 'image-apply'):
        from . import images
        with projects.operation(values[0], 'update'):
            if args.operation == 'image-preview':
                images.preview(values[0], values[1], projects.read_json(Path(values[2])), values[3])
            else:
                images.apply(*values)
    elif args.operation == 'project-limits':
        if len(values) > 2:
            with projects.operation(values[0], 'settings'):
                result = projects.project_limit_config(values[0], values[1], projects.read_json(Path(values[2])), values[3] if len(values) > 3 else None)
        else:
            result = projects.project_limit_config(*values)
        print(json.dumps(result))
    elif args.operation == 'reserve-id':
        print(projects.reserve_id(values[0], values[1], values[2] if len(values) > 2 else None))
    elif args.operation == 'release-id':
        if not (projects.METADATA / (projects.project_id(values[0]) + '.json')).exists():
            projects.release_id(values[0])
    elif args.operation == 'new-id':
        print(projects.uuid.uuid4().hex)
    elif args.operation in ('validate', 'create'):
        data = projects.validate(projects.read_json(Path(values[0])))
        if args.operation == 'create':
            with projects.operation(values[2], 'create'):
                projects.create(data, values[1], values[2], values[3] if len(values) > 3 else None, start=len(values) < 5 or values[4] != '0')
    elif args.operation == 'cleanup-temporary':
        if projects.os.geteuid() != 0:
            raise ValueError('Temporary cleanup requires the host administrator')
        from . import retention, space
        result = retention.sweep()
        result['space_records_removed'] = space.sweep()
        print(json.dumps(result))
    elif args.operation == 'security-policy':
        from .hardening import state
        print(json.dumps(state(*values)))
    elif args.operation == 'set-security-policy':
        from .hardening import convert
        convert(*values)
    elif args.operation == 'network-config':
        print(json.dumps(projects.network_config(*values)))
    elif args.operation == 'network':
        with projects.operation(values[0], 'network'):
            projects.project_network(values[0], values[1], projects.read_json(Path(values[2])), values[3] if len(values) > 3 else None)
    elif args.operation == 'update':
        with projects.operation(values[0], 'update'):
            projects.update(values[0], values[1], projects.validate(projects.read_json(Path(values[2]))), values[3] if len(values) > 3 else None)
    elif args.operation in ('dhcp-client', 'dhcp-release'):
        projects.dhcp_client(values[0], release=args.operation == 'dhcp-release')
    elif args.operation == 'notes':
        print(json.dumps(projects.notes(values[0], values[1], projects.read_text(Path(values[2])) if len(values) > 2 else None)))
    elif args.operation == 'rrddata':
        print(json.dumps(projects.rrddata(*values)))
    elif args.operation == 'management':
        print(json.dumps(projects.management(*values)))
    elif args.operation == 'pending':
        print(json.dumps(projects.pending_settings(*values)))
    elif args.operation == 'revert-settings':
        with projects.operation(values[0], 'settings'):
            projects.revert_settings(*values)
    elif args.operation == 'settings':
        with projects.operation(values[0], 'settings'):
            projects.service_settings(values[0], values[1], values[2], projects.read_json(Path(values[3])), values[4] if len(values) > 4 else None)
    elif args.operation == 'firewall-check':
        projects.firewall_check(*values)
    elif args.operation == 'delegated-check':
        print(json.dumps(projects.delegated_access(*values)))
    elif args.operation in ('console-check', 'console'):
        command = projects.console_target(*values)
        if args.operation == 'console':
            projects.node_runtime.exec_docker(command, delegated=not projects.trusted_management())
    elif args.operation == 'detail':
        print(json.dumps(projects.detail(*values)))
    elif args.operation == 'lifecycle':
        print(json.dumps(projects.lifecycle_config(*values)))
    elif args.operation == 'daemon-unmanage':
        projects.daemon_unmanage(values[0] if values else projects.current_node())
    elif args.operation == 'daemon-prepare':
        projects.daemon_prepare(values[0] if values else projects.current_node())
    elif args.operation == 'daemon-resume':
        projects.daemon_resume(values[0] if values else projects.current_node())
    elif args.operation == 'lifecycle-stop':
        projects.lifecycle_stop(*values)
    elif args.operation == 'config':
        projects.project_config(values[0], values[1], projects.read_json(Path(values[2])), values[3] if len(values) > 3 else None)
    elif args.operation == 'access-state':
        print(json.dumps(projects.access_state(*values)))
    elif args.operation == 'protection':
        print(json.dumps(projects.project_protection(*values)))
    elif args.operation == 'check-digest':
        projects.assert_configuration_digest(*values)
    elif args.operation == 'action':
        overrule = len(values) > 4 and values[4] == '1'
        if overrule and values[2] != 'force-stop':
            raise ValueError('Overrule is only available for Stop')
        with projects.operation(values[0], values[2], overrule=overrule):
            projects.action(*values[:4], destroy_data=len(values) > 5 and values[5] == '1')
    elif args.operation == 'logs':
        print(projects.project_logs(*values), end='')
    elif args.operation == 'preview':
        print(json.dumps(projects.preview(projects.read_json(Path(values[0])), values[1] if len(values) > 1 and values[1] else None,
                                          resources_only=len(values) > 2 and values[2] == 'resources')))
    elif args.operation == 'tags':
        print(json.dumps(projects.project_tags(*values)))
    elif args.operation == 'service-action':
        overrule = len(values) > 5 and values[5] == '1'
        if overrule and values[3] != 'force-stop':
            raise ValueError('Overrule is only available for Stop')
        with projects.operation(values[0], values[3], overrule=overrule, service=values[2]):
            projects.service_action(*values[:5])
    elif args.operation == 'reset-settings':
        with projects.operation(values[0], 'settings'):
            projects.reset_settings(*values)
    elif args.operation == 'snapshot-config':
        print(json.dumps(projects.snapshot_config(*values)))
    elif args.operation == 'snapshot-edit':
        with projects.operation(values[0], 'snapshot-edit'):
            projects.snapshot_edit(*values)
    elif args.operation == 'snapshots':
        print(json.dumps(projects.snapshot_list(*values)))
    elif args.operation == 'snapshot':
        with projects.operation(values[0], 'snapshot-' + values[2]):
            projects.project_snapshot(*values)
    elif args.operation == 'storage-config':
        projects.detail(*values)
        print(json.dumps(projects.storage_config(values[0])))
    elif args.operation == 'storage-volume-state':
        print(json.dumps(projects.storage_volume_state(*values)))
    elif args.operation == 'delete-storage-volume':
        projects.delete_storage_volume(*values)
    elif args.operation == 'purge-retained-storage':
        projects.purge_retained_storage(*values)
    elif args.operation == 'storage-limit':
        with projects.operation(values[0], 'settings'):
            projects.project_storage_limit(*values)
    elif args.operation == 'diagnostics':
        print(json.dumps(projects.diagnostics(*values)))
    elif args.operation == 'restore':
        projects.restore(projects.current_node())
    elif args.operation == 'refresh':
        projects.refresh(projects.current_node())
