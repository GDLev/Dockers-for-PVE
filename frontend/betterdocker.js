/* ExtJS 6 callParent reads Function.caller: these methods must remain non-strict. */
(function () {
    if (!window.PVE || !PVE.window || !PVE.window.Wizard || !PVE.StdWorkspace) {
        console.error('BetterDocker: unsupported Proxmox UI'); return;
    }
    const field = (name, label, value, extra) => Object.assign({
        xtype: 'textfield', name, fieldLabel: label, value, labelWidth: 125, anchor: '100%',
    }, extra || {});
    const request = options => Proxmox.Utils.API2Request(Object.assign({
        failure: response => Ext.Msg.alert('Docker', response.htmlStatus),
    }, options));
    const task = response => Ext.create('Proxmox.window.TaskViewer', { autoShow: true, upid: response.result.data });
    const deployment = values => {
        const common = { name: values.name, mode: values.mode, environment: values.environment || '',
            bridge: values.bridge, address: values.address || '', ipv4mode: values.ipv4mode,
            hwaddr: values.hwaddr || '', mtu: values.mtu || '' };
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
    PVE.tree.ResourceTree.typeDefaults.docker = { iconCls: 'fa bd-docker-icon', text: 'Docker' };
    const originalIcon = PVE.Utils.get_object_icon_class;
    PVE.Utils.get_object_icon_class = function (type, record) {
        if (type === 'docker') { return 'fa bd-docker-icon ' + (record.status || ''); }
        const icon = originalIcon.apply(this, arguments);
        // Existing LXC guests from the first release remain manageable as LXC.
        return record && record.type === 'lxc' && String(record.tags || '').split(';').includes('betterdocker') &&
            typeof icon === 'string' ? icon.replace('fa-cube', 'bd-docker-icon') : icon;
    };
    const originalAdd = PVE.tree.ResourceTree.prototype.addChildSorted;
    Ext.override(PVE.tree.ResourceTree, {
        addChildSorted: function (node, info) {
            if (info.type === 'docker') { info.text = info.name; }
            return originalAdd.call(this, node, info);
        },
    });

    Ext.define('PVE.betterdocker.CreateWizard', {
        extend: 'PVE.window.Wizard', subject: 'Docker', title: 'Create Docker', width: 880, height: 700,
        initComponent: function () {
            const me = this;
            const defaults = me.deployment || {};
            if (me.projectId) { me.title = 'Edit Docker deployment'; }
            me.items = [{
                xtype: 'inputpanel', title: 'Deployment',
                items: [
                    field('nodename', 'Node', me.nodename || Proxmox.NodeName, { xtype: 'pveNodeSelector', allowBlank: false, readOnly: Boolean(me.projectId),
                        listeners: { change: function (_, value) {
                            const wizard = this.up('window');
                            const bridge = wizard && wizard.down('[name=bridge]');
                            if (bridge) { bridge.setNodename(value); }
                        } } }),
                    field('name', 'Project name', defaults.name || '', { allowBlank: false, readOnly: Boolean(me.projectId) }),
                    field('mode', 'Deploy from', defaults.mode || 'compose', { xtype: 'combobox', editable: false,
                        queryMode: 'local', allowBlank: false,
                        store: [['compose', 'Docker Compose'], ['image', 'Docker image']],
                        listeners: { change: updateMode, afterrender: updateMode } }),
                    { xtype: 'container', itemId: 'composeFields', layout: 'anchor', items: [
                        field('compose', 'Compose YAML', defaults.compose || '', { xtype: 'textarea', height: 285, allowBlank: false,
                            emptyText: 'services:\n  web:\n    image: nginx:stable\n    restart: unless-stopped\n    ports:\n      - "8080:80"' }),
                    ] },
                    { xtype: 'container', itemId: 'imageFields', hidden: true, layout: 'anchor', items: [
                        field('image', 'Image', defaults.image || '', { disabled: true, allowBlank: false, emptyText: 'nginx:stable' }),
                        field('ports', 'Published ports', defaults.ports || '', { xtype: 'textarea', disabled: true, height: 110,
                            emptyText: '8080:80\n5353:53/udp' }),
                    ] },
                    field('environment', '.env (optional)', defaults.environment || '', { xtype: 'textarea', height: 75,
                        emptyText: 'APP_PASSWORD=your-value' }),
                ],
            }, {
                xtype: 'inputpanel', title: 'Network',
                column1: [
                    field('bridge', 'Bridge', defaults.bridge || 'vmbr0', { xtype: 'PVE.form.BridgeSelector',
                        nodename: me.nodename || Proxmox.NodeName, bridgeType: 'bridge', allowBlank: false, readOnly: Boolean(me.projectId) }),
                    field('hwaddr', 'MAC address', defaults.hwaddr || '', { readOnly: Boolean(me.projectId),
                        emptyText: 'auto', validator: value => !value || /^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$/.test(value) || 'Invalid MAC address' }),
                    field('mtu', 'MTU', defaults.mtu || '', { xtype: 'numberfield', readOnly: Boolean(me.projectId),
                        minValue: 576, maxValue: 9216, allowDecimals: false, emptyText: 'Same as bridge' }),
                ],
                column2: [
                    { xtype: 'radiogroup', fieldLabel: 'IPv4', columns: 2,
                        items: [
                            { xtype: 'radiofield', name: 'ipv4mode', inputValue: 'static', boxLabel: 'Static',
                                checked: (defaults.ipv4mode || (defaults.address ? 'static' : 'dhcp')) === 'static', readOnly: Boolean(me.projectId) },
                            { xtype: 'radiofield', name: 'ipv4mode', inputValue: 'dhcp', boxLabel: 'DHCP',
                                checked: (defaults.ipv4mode || (defaults.address ? 'static' : 'dhcp')) === 'dhcp', readOnly: Boolean(me.projectId) },
                        ], listeners: { change: updateNetwork, afterrender: updateNetwork } },
                    field('address', 'IPv4/CIDR', defaults.cidr || defaults.address || '', { allowBlank: false,
                        disabled: (defaults.ipv4mode || (defaults.address ? 'static' : 'dhcp')) === 'dhcp',
                        readOnly: Boolean(me.projectId), emptyText: '192.0.2.50/24' }),
                    { xtype: 'displayfield', fieldLabel: 'Gateway (IPv4)', value: 'Inherited from host routing' },
                ],
                columnB: [
                    { xtype: 'displayfield', value: 'DHCP requests an address for the project MAC on a separate interface. Static IPv4 must be unused and within the bridge subnet. Host gateway and DNS settings remain unchanged.' },
                    { xtype: 'displayfield', value: 'All ports from Compose will bind to the project IP. Services in one project share this IP through their published ports.' },
                ],
            }, {
                title: 'Review', layout: 'fit', items: [{ xtype: 'grid',
                    store: { fields: ['key', 'value'], data: [] },
                    columns: [{ text: 'Setting', dataIndex: 'key', width: 170 },
                        { text: 'Value', dataIndex: 'value', flex: 1, renderer: Ext.htmlEncode }],
                }],
                listeners: { show: function () {
                    const values = this.up('window').getValues();
                    const data = [{ key: 'Node', value: values.nodename }, { key: 'Project', value: values.name },
                        { key: 'Deployment', value: values.mode === 'compose' ? 'Docker Compose' : values.image },
                        { key: 'Bridge', value: values.bridge }, { key: 'IPv4', value: values.ipv4mode === 'dhcp' ? 'Assigned by DHCP' : values.address },
                        { key: 'MAC address', value: values.hwaddr || 'Auto-generated' },
                        { key: 'Published ports', value: values.mode === 'compose' ? 'Read from Compose YAML' : values.ports || 'None' }];
                    this.down('grid').getStore().loadData(data);
                } },
                onSubmit: function () {
                    const wizard = this.up('window');
                    const values = wizard.getValues();
                    request({ url: '/nodes/' + encodeURIComponent(values.nodename) + '/docker' + (wizard.projectId ? '/' + wizard.projectId : ''),
                        method: wizard.projectId ? 'PUT' : 'POST',
                        params: { deployment: JSON.stringify(deployment(values)) }, waitMsgTarget: wizard,
                        success: response => { task(response); wizard.close(); } });
                },
            }];
            me.callParent();
            const help = me.down('#help');
            if (help) { help.hide(); }
            me.query('proxmoxcheckbox').forEach(item => item.hide());
            [['next', 'Next'], ['back', 'Back'], ['submit', 'Deploy']].forEach(([id, text]) => {
                const button = me.down('#' + id);
                if (button) { button.setText(text); }
            });
        },
    });

    Ext.define('PVE.betterdocker.Config', {
        extend: 'Ext.panel.Panel', alias: 'widget.pveBetterDockerConfig', layout: 'fit',
        initComponent: function () {
            const me = this;
            const info = me.pveSelNode.data;
            const identifier = info.id.split('/')[1];
            const url = '/nodes/' + encodeURIComponent(info.node) + '/docker/' + identifier;
            const reload = () => request({ url, method: 'GET', success: response => {
                const data = response.result.data;
                me.down('#summary').setValue(data.name + ' · ' + data.status + ' · ' + data.address + ' · ' + data.bridge);
                me.down('#source').setValue(data.deployment.mode === 'compose' ? data.deployment.compose :
                    'Image: ' + data.deployment.image + '\nPublished ports:\n' + (data.deployment.ports || 'None'));
            } });
            const act = operation => request({ url: url + '/action', method: 'POST', params: { action: operation },
                waitMsgTarget: me, success: task });
            me.title = 'Docker — ' + info.name;
            me.tbar = [
                { text: 'Start', iconCls: 'fa fa-play', handler: () => act('start') },
                { text: 'Stop', iconCls: 'fa fa-stop', handler: () => act('stop') },
                { text: 'Restart', iconCls: 'fa fa-refresh', handler: () => act('restart') },
                { text: 'Refresh', handler: reload },
                { text: 'Edit deployment', handler: () => request({ url, method: 'GET', success: response => {
                    Ext.create('PVE.betterdocker.CreateWizard', { projectId: identifier, nodename: info.node,
                        deployment: response.result.data.deployment }).show();
                } }) }, '->',
                { text: 'Remove', iconCls: 'fa fa-trash', handler: () => Ext.Msg.confirm('Remove Docker project',
                    'Remove this project? Volumes and bind-mount data are retained.', answer => { if (answer === 'yes') { act('delete'); } }) },
            ];
            me.items = [{ xtype: 'tabpanel', items: [{ title: 'Deployment', xtype: 'panel', bodyPadding: 12,
                layout: 'anchor', items: [
                    { xtype: 'displayfield', itemId: 'summary', anchor: '100%', fieldLabel: 'Status', renderer: Ext.htmlEncode },
                    { xtype: 'textarea', itemId: 'source', anchor: '100%', height: 390, readOnly: true, fieldLabel: 'Deployment' },
                    { xtype: 'displayfield', value: 'Docker projects use the host engine. VM/CT backup and migration actions do not apply.' },
                ] }, { title: 'Logs', xtype: 'panel', layout: 'fit',
                    items: [{ xtype: 'textarea', itemId: 'logs', readOnly: true }],
                    listeners: { activate: () => request({ url: url + '/logs', method: 'GET',
                        success: response => me.down('#logs').setValue(response.result.data) }) },
                } ] }];
            me.callParent();
            me.on('afterrender', reload, me, { single: true });
        },
    });

    const originalInit = PVE.StdWorkspace.prototype.initComponent;
    const originalContent = PVE.StdWorkspace.prototype.setContent;
    Ext.override(PVE.StdWorkspace, {
        setContent: function (comp) {
            if (comp && comp.pveSelNode && comp.pveSelNode.data.type === 'docker') {
                comp.xtype = 'pveBetterDockerConfig';
            }
            return originalContent.call(this, comp);
        },
        initComponent: function () {
            originalInit.apply(this, arguments);
            try {
                const user = this.down('#userinfo');
                if (!user || !user.ownerCt) { return; }
                const toolbar = user.ownerCt;
                toolbar.insert(toolbar.items.indexOf(user), {
                    xtype: 'button', text: 'Create Docker', iconCls: 'fa bd-docker-icon',
                    margin: '3 5 0 0', baseCls: 'x-btn', disabled: Proxmox.UserName !== 'root@pam',
                    handler: () => Ext.create('PVE.betterdocker.CreateWizard').show(),
                });
            } catch (error) { console.error('BetterDocker: cannot add toolbar button', error); }
        },
    });
})();
