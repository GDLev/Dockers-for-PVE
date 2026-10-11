Ext.define('PVE.betterdocker.Migrate', {
    extend: 'Proxmox.window.Edit', isCreate: true, autoLoad: false,
    method: 'POST', showTaskViewer: true, onlineHelp: 'chapter_pvecm',
    initComponent: function () {
        const me = this;
        me.title = translate('Migrate') + ' DCT ' + PVE.Utils.getFormattedGuestIdentifier(me.vmid, me.vmname);
        me.submitText = translate('Migrate');
        me.url = '/api2/extjs/nodes/' + encodeURIComponent(me.nodename) + '/docker/' + me.projectId + '/migrate';
        me.items = [{ xtype: 'inputpanel', items: [
            { xtype: 'pveNodeSelector', name: 'target', fieldLabel: translate('Target node'), allowBlank: false,
                disallowedNodes: [me.nodename], onlineValidator: true },
            { xtype: 'pveStorageSelector', name: 'storage', fieldLabel: translate('Target Storage'), storageContent: 'rootdir',
                allowBlank: true, hidden: Boolean(me.haManaged), disabled: Boolean(me.haManaged), emptyText: translate('Same as source'), storageType: ['dir', 'lvmthin', 'zfspool'] },
            { xtype: 'proxmoxcheckbox', name: 'restart', fieldLabel: translate('Restart Mode'),
                checked: Boolean(me.running), hidden: !me.running || Boolean(me.haManaged), disabled: !me.running || Boolean(me.haManaged) },
            { xtype: 'numberfield', name: 'timeout', fieldLabel: translate('Shutdown timeout'), value: 180, minValue: 1, maxValue: 86400, allowDecimals: false,
                hidden: !me.running || Boolean(me.haManaged), disabled: !me.running || Boolean(me.haManaged) },
        ], onGetValues: values => {
            if (me.haManaged || !values.storage) { delete values.storage; }
            if (me.digest) { values.digest = me.digest; }
            return values;
        } }];
        me.callParent();
        me.down('[name=target]').on('change', (_, node) => me.down('[name=storage]').setNodename(node));
    },
});

Ext.define('PVE.betterdocker.StateRestore', {
    extend: 'Proxmox.window.Edit', alias: 'widget.pveBetterDockerStateRestore',
    isCreate: true, autoLoad: false,
    onlineHelp: 'chapter_vzdump', method: 'POST',
    initComponent: function () {
        const me = this, clone = Boolean(me.projectId);
        me.showTaskViewer = !clone;
        me.submitText = translate(clone ? 'Clone' : 'Restore');
        if (clone) { me.apiCallDone = (success, response) => { if (success) { trackTask(response, me.ownerPanel); } }; }
        me.title = clone ? translate('Clone') + ' DCT ' + (me.isTemplate ? translate('Template') + ' ' : '') +
            PVE.Utils.getFormattedGuestIdentifier(me.vmid, me.guestname) : translate('Restore') + ': DCT';
        me.url = '/api2/extjs/nodes/' + encodeURIComponent(me.nodename) + '/docker' +
            (clone ? '/' + me.projectId + '/clone' : '');
        me.items = [{ xtype: 'inputpanel',
            column1: [
                { xtype: 'pveGuestIDSelector', name: 'newid', fieldLabel: translate('DCT ID'), guestType: 'docker',
                    loadNextFreeID: true, allowBlank: false },
                field('name', 'Name', me.guestname || '', { allowBlank: false, maxLength: 63 }),
                { xtype: 'pveStorageSelector', name: 'storage', nodename: me.nodename, fieldLabel: translate('Storage'),
                    storageContent: 'rootdir', allowBlank: true, emptyText: translate('Default'),
                    storageType: ['dir', 'lvmthin', 'zfspool'] },
            ],
            column2: [
                { xtype: 'pvePoolSelector', name: 'pool', fieldLabel: translate('Resource Pool'), allowBlank: true },
                { xtype: 'pveBetterDockerBridgeSelector', name: 'bridge', nodename: me.nodename, bridgeType: 'any_bridge',
                    fieldLabel: translate('Bridge'), value: 'vmbr0', allowBlank: false },
                { xtype: 'radiogroup', fieldLabel: translate('IPv4'), columns: 2,
                    items: [
                        { xtype: 'radiofield', name: 'ipv4mode', inputValue: 'static', boxLabel: translate('Static'), checked: true },
                        { xtype: 'radiofield', name: 'ipv4mode', inputValue: 'dhcp', boxLabel: translate('DHCP') },
                    ], listeners: { change: group => {
                        group.up('window').down('[name=cidr]').setDisabled(group.getValue().ipv4mode === 'dhcp');
                    } } },
                field('cidr', 'IPv4/CIDR', '', { vtype: 'IPCIDRAddress', allowBlank: false }),
            ],
            onGetValues: function (values) {
                const deployment = { name: values.name, bridge: values.bridge, ipv4mode: values.ipv4mode };
                if (values.ipv4mode !== 'dhcp') { deployment.cidr = values.cidr; }
                const result = { newid: values.newid, deployment: JSON.stringify(deployment) };
                if (values.storage) { result.storage = values.storage; }
                if (values.pool) { result.pool = values.pool; }
                if (clone) { if (me.digest) { result.digest = me.digest; } }
                else { result.archive = me.archive; }
                return result;
            },
        }];
        me.callParent();
    },
});
