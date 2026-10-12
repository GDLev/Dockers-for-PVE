const deployment = values => {
    const common = { name: values.name, mode: values.mode, environment: values.environment || '',
        bridge: values.bridge, address: values.address || '', ipv4mode: values.ipv4mode,
        hwaddr: values.hwaddr || '', mtu: values.mtu || '', firewall: Number(values.firewall) === 1 ? 1 : 0 };
    if (values.vmid) { common.vmid = Number(values.vmid); }
    if (values.interfaces) { common.interfaces = values.interfaces; }
    return Object.assign(common, values.mode === 'compose' ? { compose: values.compose } :
        { image: values.image, ports: values.ports || '' });
};
function updateMode(combo) {
    const wizard = combo.up('window');
    if (!wizard) { return; }
    ['compose', 'image'].forEach(mode => {
        const group = wizard.down('#' + mode + 'Fields');
        if (!group) { return; }
        const active = combo.getValue() === mode;
        group.setHidden(!active);
        group.query('field').forEach(item => item.setDisabled(!active));
    });
}
function updateNetwork(group) {
    const wizard = group.up('window');
    if (!wizard) { return; }
    const address = wizard.down('[name=address]');
    if (address) { address.setDisabled(group.getValue().ipv4mode !== 'static'); }
}
Ext.define('PVE.betterdocker.CreateWizard', {
    extend: 'PVE.window.Wizard', subject: 'Docker', title: translate('Create DCT'), width: 720, height: 540, resizable: false, constrain: true,
    initComponent: function () {
        const me = this;
        const defaults = me.deployment || {};
        if (typeof window.innerHeight === 'number') { if (typeof me.height === 'number') { me.height = Math.min(me.height, window.innerHeight - 40); } me.width = Math.min(me.width, window.innerWidth - 40); }
        if (me.projectId) { me.title = 'Edit Docker deployment'; }
        me.items = [{
            xtype: 'inputpanel', title: translate('General'), onlineHelp: 'chapter_system_administration',
            column1: [
                field('nodename', 'Node', me.nodename || Proxmox.NodeName, { xtype: 'pveNodeSelector', allowBlank: false, readOnly: Boolean(me.projectId),
                    listeners: { change: function (_, value) {
                        const wizard = this.up('window');
                        const bridge = wizard && wizard.down('[name=bridge]');
                        if (bridge) { bridge.setNodename(value); }
                    } } }),
                field('vmid', 'DCT ID', defaults.vmid || me.vmid || '', { xtype: 'pveGuestIDSelector',
                    guestType: 'docker', loadNextFreeID: !defaults.vmid && !me.vmid && !me.projectId,
                    validateExists: me.projectId ? undefined : false, allowBlank: false, readOnly: Boolean(me.projectId) }),
                field('name', 'Project name', defaults.name || '', { allowBlank: false, readOnly: Boolean(me.projectId),
                    maxLength: 63, regex: /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$/,
                    regexText: translate('Use letters, numbers, dots, underscores or hyphens; start with a letter or number.') }),
            ],
            column2: [
                { xtype: 'pvePoolSelector', name: 'pool', fieldLabel: translate('Resource Pool'), allowBlank: true, hidden: Boolean(me.projectId), disabled: Boolean(me.projectId) },
                field('mode', 'Deploy from', defaults.mode || 'compose', { xtype: 'combobox', editable: false,
                    queryMode: 'local', allowBlank: false,
                    store: [['compose', 'Docker Compose'], ['image', 'Docker image']],
                    listeners: { change: updateMode, afterrender: updateMode } }),
            ],
        }, {
            xtype: 'inputpanel', title: translate('Deployment'), onlineHelp: 'chapter_system_administration', scrollable: true,
            items: [
                { xtype: 'container', itemId: 'composeFields', layout: 'anchor', items: [
                    field('compose', 'Compose YAML', defaults.compose || '', { xtype: 'pveBetterDockerCodeArea', height: 230, allowBlank: false, maxLength: 240000,
                        emptyText: 'services:\n  web:\n    image: nginx:stable' }),
                    importFile('Load YAML', 'compose', 240000),
                ] },
                { xtype: 'container', itemId: 'imageFields', hidden: true, layout: 'anchor', items: [
                    imageField(defaults.image || '', { disabled: true }),
                    field('ports', 'Published ports', defaults.ports || '', { xtype: 'textarea', disabled: true, height: 110,
                        emptyText: '8080:80\n5353:53/udp' }),
                ] },
            ],
            advancedItems: [
                field('environment', '.env', defaults.environment || '', { xtype: 'pveBetterDockerCodeArea', height: 65, maxLength: 16000 }),
                importFile('Load .env', 'environment', 16000),
            ],
        }, {
            xtype: 'inputpanel', title: translate('Network'), onlineHelp: 'chapter_system_administration',
            column1: [
                field('bridge', 'Bridge', defaults.bridge || 'vmbr0', { xtype: 'pveBetterDockerBridgeSelector',
                    nodename: me.nodename || Proxmox.NodeName, bridgeType: 'any_bridge', allowBlank: false, readOnly: Boolean(me.projectId) }),
                { xtype: 'proxmoxcheckbox', name: 'firewall', fieldLabel: gettext('Firewall'),
                    checked: Boolean(defaults.firewall), uncheckedValue: 0, readOnly: Boolean(me.projectId) },
                field('hwaddr', 'MAC address', defaults.hwaddr || '', { readOnly: Boolean(me.projectId),
                    emptyText: 'auto', vtype: 'MacAddress' }),
            ],
            column2: [
                { xtype: 'radiogroup', fieldLabel: translate('IPv4'), columns: 3,
                    items: [
                        { xtype: 'radiofield', name: 'ipv4mode', inputValue: 'static', boxLabel: translate('Static'),
                            checked: (defaults.ipv4mode || (defaults.address ? 'static' : 'dhcp')) === 'static', readOnly: Boolean(me.projectId) },
                        { xtype: 'radiofield', name: 'ipv4mode', inputValue: 'dhcp', boxLabel: translate('DHCP'),
                            checked: (defaults.ipv4mode || (defaults.address ? 'static' : 'dhcp')) === 'dhcp', readOnly: Boolean(me.projectId) },
                        { xtype: 'radiofield', name: 'ipv4mode', inputValue: 'none', boxLabel: translate('None'), checked: defaults.ipv4mode === 'none', readOnly: Boolean(me.projectId) },
                    ], listeners: { change: updateNetwork, afterrender: updateNetwork } },
                field('address', 'IPv4/CIDR', defaults.cidr || defaults.address || '', { allowBlank: false, vtype: 'IPCIDRAddress',
                    disabled: (defaults.ipv4mode || (defaults.address ? 'static' : 'dhcp')) !== 'static',
                    readOnly: Boolean(me.projectId), emptyText: '192.0.2.50/24' }),
                { xtype: 'displayfield', fieldLabel: translate('Gateway (IPv4)'), value: translate('Inherited from host routing') },
            ],
            advancedColumn1: [field('mtu', 'MTU', defaults.mtu || '', { xtype: 'numberfield', readOnly: Boolean(me.projectId),
                minValue: 576, maxValue: 9216, allowDecimals: false, emptyText: translate('Same as bridge') })],
        }, {
            title: translate('Confirm'), onlineHelp: 'chapter_system_administration', layout: 'fit',
            dockedItems: [{ xtype: 'proxmoxcheckbox', name: 'start', dock: 'bottom', margin: '5 0 0 0',
                boxLabel: gettext('Start after created'), uncheckedValue: 0, hidden: Boolean(me.projectId), disabled: Boolean(me.projectId) }],
            items: [{ xtype: 'grid', itemId: 'reviewGrid',
                store: { fields: ['key', 'value'], data: [], sorters: [{ property: 'key', direction: 'ASC' }] },
                columns: [{ text: translate('Name'), dataIndex: 'key', width: 150, renderer: Ext.htmlEncode },
                    { text: translate('Value'), dataIndex: 'value', flex: 1, renderer: Ext.htmlEncode }],
            }],
            listeners: { show: function () {
                const values = this.up('window').getValues();
                const data = [
                    ['Node', values.nodename], ['DCT ID', values.vmid], ['Project name', values.name],
                    ['Deploy from', values.mode === 'compose' ? 'Docker Compose' : 'Docker image'],
                    ['Bridge', values.bridge], ['IPv4', values.ipv4mode === 'dhcp' ? 'DHCP' : values.address],
                    ['MAC address', values.hwaddr || 'Auto-generated'],
                    ['MTU', values.mtu || translate('Same as bridge')],
                    ['Firewall', Proxmox.Utils.format_boolean(Number(values.firewall) === 1)],
                ];
                if (values.pool) { data.push(['Resource Pool', values.pool]); }
                if (values.mode === 'image') { data.push(['Image', values.image], ['Published ports', values.ports || translate('None')]); }
                else { data.push(['Published ports', translate('Read from Compose YAML')]); }
                // Compose and .env may contain secrets; keep them out of the review grid.
                this.down('grid').getStore().loadData(data.map(([key, value]) => ({ key: translate(key), value })));
            } },
            onSubmit: function () {
                const wizard = this.up('window');
                if (wizard.dockerSubmitting || !wizard.down('form').getForm().isValid()) { return; }
                const values = wizard.getValues();
                wizard.dockerSubmitting = true;
                // The create endpoint validates before allocating a VMID or starting its task.
                request({ url: '/nodes/' + encodeURIComponent(values.nodename) + '/docker' + (wizard.projectId ? '/' + wizard.projectId : ''),
                    method: wizard.projectId ? 'PUT' : 'POST',
                    params: Object.assign({ deployment: JSON.stringify(deployment(Object.assign({}, values, me.projectId && defaults.interfaces ? { interfaces: defaults.interfaces } : {}))) }, !wizard.projectId ? { start: Number(values.start) === 1 ? 1 : 0, ...(values.pool ? { pool: values.pool } : {}) } : {}), waitMsgTarget: wizard,
                    success: response => { task(response); wizard.close(); } });
            },
        }];
        me.callParent();
        const helpButton = me.down('#help');
        if (helpButton) { helpButton.setHandler(help); }
        [['next', 'Next'], ['back', 'Back'], ['submit', me.projectId ? 'Apply' : 'Finish']].forEach(([id, text]) => {
            const button = me.down('#' + id);
            if (button) { button.setText(translate(text)); }
        });
    },
});
Ext.define('PVE.betterdocker.DeploymentEdit', {
    extend: 'Proxmox.window.Edit', width: 600, resizable: false, constrain: true, onlineHelp: 'chapter_system_administration',
    initComponent: function () {
        const me = this, defaults = me.deployment, digest = me.digest;
        if (typeof window.innerHeight === 'number') { if (typeof me.height === 'number') { me.height = Math.min(me.height, window.innerHeight - 40); } me.width = Math.min(me.width, window.innerWidth - 40); }
        me.title = translate('Edit deployment') + ': DCT ' + me.vmid;
        me.url = '/nodes/' + encodeURIComponent(me.nodename) + '/docker/' + me.projectId;
        me.items = [{ xtype: 'inputpanel', items: [
            field('mode', 'Deploy from', defaults.mode, { xtype: 'combobox', editable: false, queryMode: 'local',
                store: [['compose', 'Docker Compose'], ['image', 'Docker image']], listeners: { change: updateMode, afterrender: updateMode } }),
            { xtype: 'container', itemId: 'composeFields', layout: 'anchor', items: [
                field('compose', 'Compose YAML', defaults.compose || '', { xtype: 'pveBetterDockerCodeArea', height: 230, allowBlank: false, maxLength: 240000 }),
                importFile('Load YAML', 'compose', 240000)] },
            { xtype: 'container', itemId: 'imageFields', layout: 'anchor', items: [imageField(defaults.image || ''),
                field('ports', 'Published ports', defaults.ports || '', { xtype: 'textarea', height: 120 })] },
        ], advancedItems: [
            field('environment', '.env', defaults.environment || '', { xtype: 'pveBetterDockerCodeArea', height: 65, maxLength: 16000 }),
            importFile('Load .env', 'environment', 16000),
        ] }];
        me.submit = () => {
            if (!me.formPanel.getForm().isValid()) { return; }
            const changed = me.getValues();
            const payload = JSON.stringify(deployment(Object.assign({}, defaults, changed, { vmid: me.vmid })));
            request({ url: '/nodes/' + encodeURIComponent(me.nodename) + '/docker', method: 'PUT',
                params: { id: me.projectId, deployment: payload }, waitMsgTarget: me,
                success: () => request({ url: me.url, method: 'PUT', params: { deployment: payload, digest }, waitMsgTarget: me,
                    success: response => { trackTask(response, me.ownerPanel); task(response); me.close(); } }) });
        };
        me.callParent();
        configureHelp(me);
    },
});
