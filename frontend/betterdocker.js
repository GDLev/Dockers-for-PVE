(function () {
    if (!window.PVE || !PVE.window || !PVE.window.Wizard || !PVE.StdWorkspace) {
        console.error('BetterDocker: unsupported Proxmox UI'); return;
    }

    const translate = value => {
        const native = typeof gettext === 'function' ? gettext(value) : value;
        if (native !== value) { return native; }
        const locales = PVE.betterdockerLocales || {};
        const active = Object.values(locales).find(locale => locale.isActive && locale.isActive());
        return active?.messages[value] || locales.en?.messages[value] || value;
    };
    const help = () => Ext.create('Ext.window.Window', { title: translate('Docker help'), modal: true,
        width: 660, height: 460, layout: 'fit', items: [{ xtype: 'component', scrollable: true, padding: 15,
            html: [
                ["Resources", "Limits apply per container. Restore Compose removes GUI overrides. Zero CPU, memory or PID limit means unlimited; with a memory limit, swap 0 disables swap and -1 is unlimited."],
                ["Network", "Network settings are fixed after creation. Published ports use the DCT address."],
                ["Container disk limit", "Requires overlay2 on XFS with project quotas. Volumes and bind mounts are excluded."],
                ["Project data disk", "Stop services before creating or enlarging the disk. Original data is kept. A mount failure can prevent Docker from starting on the node."],
                ["Snapshots", "Full copies of configuration, container filesystems and owned data. Services are frozen during capture. RAM, logs and external or shared writable mounts are excluded. Rollback leaves DCT stopped."],
                ["Remove", "Persistent data and deployment files are retained. Native VM/CT backup, HA and migration are unavailable."],
            ].map(([label, text]) => '<p><strong>' + Ext.htmlEncode(translate(label)) + '</strong><br>' +
                Ext.htmlEncode(translate(text)) + '</p>').join('') +
                '<p><a href="https://github.com/GDLev/Proxmox-BetterDockers" target="_blank" rel="noopener">' + Ext.htmlEncode(translate('Documentation')) + '</a></p>',
        }] }).show();
    function configureHelp(win) {
        const button = win.down('proxmoxHelpButton');
        if (button) { button.setHandler(help); }
    }
    const download = (name, value) => {
        const link = document.createElement('a');
        const url = URL.createObjectURL(new Blob([value], { type: 'text/plain;charset=utf-8' }));
        link.href = url; link.download = name; link.click(); URL.revokeObjectURL(url);
    };
    const importFile = (label, name, maximum) => ({ xclass: 'Ext.form.field.FileButton', text: translate(label),
        listeners: { change: async function () {
            const file = this.fileInputEl.dom.files[0];
            if (!file) { return; }
            if (file.size > maximum) { Ext.Msg.alert(translate('Error'), translate('File is too large')); this.reset(true); return; }
            try {
                const content = await file.text();
                if (!this.destroyed) { this.up('window').down('[name=' + name + ']').setValue(content); }
            } catch (_) {
                if (!this.destroyed) { Ext.Msg.alert(translate('Error'), translate('Could not read file')); }
            } finally {
                if (!this.destroyed) { this.reset(true); }
            }
        } } });
    const importRow = (label, name, maximum) => ({ xtype: 'container', layout: 'hbox', margin: '0 0 5 0',
        items: [importFile(label, name, maximum)] });
    const statusText = value => translate(({ running: 'Running', stopped: 'Stopped', partial: 'Partially running',
        creating: 'Deploying', error: 'Deployment failed', 'network-error': 'Network error', unknown: 'Unknown' })[value] || value);
    const trackTask = (response, target) => {
        const upid = response.result.data;
        if (!target || !target.on || !upid) { return; }
        target.pendingTask = upid;
        const node = target.pveSelNode && target.pveSelNode.data.node;
        if (!node) { return; }
        const poll = () => {
            if (target.destroyed) { clearInterval(timer); return; }
            request({ url: '/nodes/' + encodeURIComponent(node) + '/tasks/' + encodeURIComponent(upid) + '/status',
                method: 'GET', failure: result => {
                    if (!target.destroyed) { Proxmox.Utils.setErrorMask(target, result.htmlStatus); }
                }, success: result => {
                    if (target.destroyed) { return; }
                    Proxmox.Utils.setErrorMask(target, false);
                    if (result.result.data.status === 'stopped') {
                        clearInterval(timer); target.pendingTask = null;
                        if (target.reloadDocker) { target.reloadDocker(); }
                        if (result.result.data.exitstatus !== 'OK') { task(response); }
                    }
                } });
        };
        const timer = setInterval(poll, 1500);
        target.on('beforedestroy', () => clearInterval(timer), target, { single: true });
        if (target.reloadDocker) { target.reloadDocker(); }
    };

    const field = (name, label, value, extra) => Object.assign({
        xtype: 'textfield', name, fieldLabel: translate(label), value, labelWidth: 100, anchor: '100%',
    }, extra || {});
    const request = options => Proxmox.Utils.API2Request(Object.assign({
        failure: response => Ext.Msg.alert(translate('Error'), response.htmlStatus),
    }, options));
    const task = response => Ext.create('Proxmox.window.TaskViewer', { autoShow: true, upid: response.result.data });
    const dockerTaskLabels = { create: 'Deploy', update: 'Edit deployment', start: 'Start', stop: 'Shutdown',
        restart: 'Restart', delete: 'Remove', settings: 'Edit resources / options', console: 'Console', snapshotcreate: 'Take Snapshot', snapshotrestore: 'Rollback', snapshotdelete: 'Remove Snapshot' };
    const dockerTaskDescriptions = { betterdocker: () => 'Docker - Deployment action (legacy)' };
    Object.entries(dockerTaskLabels).forEach(([action, label]) => {
        dockerTaskDescriptions['betterdocker' + action] = (type, id) =>
            (/^\d+$/.test(String(id)) ? 'DCT ' + id : 'Docker') + ' - ' + translate(label);
    });
    Proxmox.Utils.override_task_descriptions(dockerTaskDescriptions);
    const dockerAction = (info, operation, target) => {
        const execute = () => request({
            url: '/nodes/' + encodeURIComponent(info.node) + '/docker/' + (info.realId || info.id).split('/')[1] + '/action',
            method: 'POST', params: { action: operation }, waitMsgTarget: target,
            success: response => { trackTask(response, target); if (operation === 'delete') { task(response); } },
        });
        if (operation === 'stop' || operation === 'restart') {
            const label = operation === 'stop' ? translate('Shutdown') : translate('Restart');
            const guest = (info.vmid ? 'DCT ' + info.vmid : 'Docker') + ' (' + info.name + ')';
            Ext.Msg.confirm(translate('Confirm'), Ext.htmlEncode(label + ' ' + guest + '?'), answer => {
                if (answer === 'yes') { execute(); }
            });
            return;
        }
        return execute();
    };
    const deployment = values => {
        const common = { name: values.name, mode: values.mode, environment: values.environment || '',
            bridge: values.bridge, address: values.address || '', ipv4mode: values.ipv4mode,
            hwaddr: values.hwaddr || '', mtu: values.mtu || '' };
        if (values.vmid) { common.vmid = Number(values.vmid); }
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
    PVE.tree.ResourceTree.typeDefaults.docker = { iconCls: 'fa bd-docker-icon', text: translate('Docker') };
    const originalIcon = PVE.Utils.get_object_icon_class;
    PVE.Utils.get_object_icon_class = function (type, record) {
        if (type === 'docker') { return 'fa bd-docker-icon ' + (['running', 'stopped'].includes(record.status) ? record.status : 'unknown'); }
        const icon = originalIcon.apply(this, arguments);
        // Existing LXC guests from the first release remain manageable as LXC.
        return record && record.type === 'lxc' && String(record.tags || '').split(';').includes('betterdocker') &&
            typeof icon === 'string' ? icon.replace('fa-cube', 'bd-docker-icon') : icon;
    };
    const originalAdd = PVE.tree.ResourceTree.prototype.addChildSorted;
    const originalTypeOrder = PVE.tree.ResourceTree.prototype.getTypeOrder;
    const originalSort = PVE.tree.ResourceTree.prototype.nodeSortFn;
    const guestOrder = { lxc: 0, docker: 1, qemu: 2 };
    Ext.override(PVE.tree.ResourceTree, {
        getTypeOrder: function (type) {
            if (type === 'docker') {
                return (originalTypeOrder.call(this, 'lxc') + originalTypeOrder.call(this, 'qemu')) / 2;
            }
            return originalTypeOrder.call(this, type);
        },
        nodeSortFn: function (node1, node2) {
            const a = node1.data, b = node2.data;
            const typeA = a.type === 'type' ? a.groupbyid : a.type;
            const typeB = b.type === 'type' ? b.groupbyid : b.type;
            // Keep all three guest types in a consistent order even when native guest grouping is off.
            if (!a.groupbyid === !b.groupbyid && typeA in guestOrder && typeB in guestOrder) {
                if (typeA !== typeB) { return guestOrder[typeA] - guestOrder[typeB]; }
                if (typeA === 'docker') {
                    if (a.vmid && b.vmid && a.vmid !== b.vmid) { return a.vmid - b.vmid; }
                    return String(a.name || a.text || '').localeCompare(String(b.name || b.text || '')) ||
                        String(a.id).localeCompare(String(b.id));
                }
            }
            return originalSort.call(this, node1, node2);
        },
        addChildSorted: function (node, info) {
            if (info.type === 'docker') { info.text = info.vmid ? info.vmid + ' (' + info.name + ')' : info.name; }
            return originalAdd.apply(this, arguments);
        },
    });


    Ext.define('PVE.betterdocker.CodeArea', {
        extend: 'Ext.form.field.TextArea', alias: 'widget.pveBetterDockerCodeArea', fieldCls: 'bd-source',
        initComponent: function () {
            const me = this;
            me.callParent();
            me.on('afterrender', () => { me.inputEl.dom.spellcheck = false; });
        },
    });
    Ext.define('PVE.betterdocker.CreateWizard', {
        extend: 'PVE.window.Wizard', subject: 'Docker', title: translate('Create DCT'), width: 760, height: 570, resizable: false, constrain: true,
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
                    field('vmid', 'DCT ID', defaults.vmid || me.vmid || '', { xtype: 'numberfield',
                        minValue: 100, maxValue: 999999999, allowDecimals: false, readOnly: Boolean(me.projectId),
                        emptyText: translate('Next available guest ID') }),
                    field('name', 'Project name', defaults.name || '', { allowBlank: false, readOnly: Boolean(me.projectId) }),
                ],
                column2: [
                    field('mode', 'Deploy from', defaults.mode || 'compose', { xtype: 'combobox', editable: false,
                        queryMode: 'local', allowBlank: false,
                        store: [['compose', 'Docker Compose'], ['image', 'Docker image']],
                        listeners: { change: updateMode, afterrender: updateMode } }),
                ],
            }, {
                xtype: 'inputpanel', title: translate('Deployment'), onlineHelp: 'chapter_system_administration', scrollable: true,
                items: [
                    { xtype: 'container', itemId: 'composeFields', layout: 'anchor', items: [
                        field('compose', 'Compose YAML', defaults.compose || '', { xtype: 'pveBetterDockerCodeArea', height: 230, allowBlank: false,
                            emptyText: 'services:\n  web:\n    image: nginx:stable' }),
                        importRow('Load YAML', 'compose', 240000),
                    ] },
                    { xtype: 'container', itemId: 'imageFields', hidden: true, layout: 'anchor', items: [
                        field('image', 'Image', defaults.image || '', { disabled: true, allowBlank: false, emptyText: 'nginx:stable' }),
                        field('ports', 'Published ports', defaults.ports || '', { xtype: 'textarea', disabled: true, height: 110,
                            emptyText: '8080:80\n5353:53/udp' }),
                    ] },
                ],
                advancedItems: [
                    field('environment', '.env', defaults.environment || '', { xtype: 'textarea', height: 65, fieldCls: 'bd-source' }),
                    importRow('Load .env', 'environment', 16000),
                ],
            }, {
                xtype: 'inputpanel', title: translate('Network'), onlineHelp: 'chapter_system_administration',
                column1: [
                    field('bridge', 'Bridge', defaults.bridge || 'vmbr0', { xtype: 'PVE.form.BridgeSelector',
                        nodename: me.nodename || Proxmox.NodeName, bridgeType: 'bridge', allowBlank: false, readOnly: Boolean(me.projectId) }),
                    field('hwaddr', 'MAC address', defaults.hwaddr || '', { readOnly: Boolean(me.projectId),
                        emptyText: 'auto', validator: value => !value || /^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$/.test(value) || 'Invalid MAC address' }),
                    field('mtu', 'MTU', defaults.mtu || '', { xtype: 'numberfield', readOnly: Boolean(me.projectId),
                        minValue: 576, maxValue: 9216, allowDecimals: false, emptyText: translate('Same as bridge') }),
                ],
                column2: [
                    { xtype: 'radiogroup', fieldLabel: translate('IPv4'), columns: 2,
                        items: [
                            { xtype: 'radiofield', name: 'ipv4mode', inputValue: 'static', boxLabel: translate('Static'),
                                checked: (defaults.ipv4mode || (defaults.address ? 'static' : 'dhcp')) === 'static', readOnly: Boolean(me.projectId) },
                            { xtype: 'radiofield', name: 'ipv4mode', inputValue: 'dhcp', boxLabel: translate('DHCP'),
                                checked: (defaults.ipv4mode || (defaults.address ? 'static' : 'dhcp')) === 'dhcp', readOnly: Boolean(me.projectId) },
                        ], listeners: { change: updateNetwork, afterrender: updateNetwork } },
                    field('address', 'IPv4/CIDR', defaults.cidr || defaults.address || '', { allowBlank: false,
                        disabled: (defaults.ipv4mode || (defaults.address ? 'static' : 'dhcp')) === 'dhcp',
                        readOnly: Boolean(me.projectId), emptyText: '192.0.2.50/24' }),
                    { xtype: 'displayfield', fieldLabel: translate('Gateway (IPv4)'), value: translate('Inherited from host routing') },
                ],
            }, {
                title: translate('Confirm'), onlineHelp: 'chapter_system_administration', layout: 'fit',
                validator: function () { const wizard = this.up('window'); return wizard.validatedDeployment === JSON.stringify(deployment(wizard.getValues())); }, tbar: [{ text: translate('Validate Compose'), handler: function () { this.up('window').validateDeployment(); } }], items: [{ xtype: 'grid', itemId: 'reviewGrid',
                    store: { fields: ['key', 'value'], data: [] },
                    columns: [{ text: translate('Name'), dataIndex: 'key', width: 170,
                        renderer: value => Ext.htmlEncode(value.startsWith('Service: ') ? translate('Service') + ': ' + value.slice(9) : translate(value)) },
                        { text: translate('Value'), dataIndex: 'value', flex: 1, renderer: value => Ext.htmlEncode(translate(value)) }],
                }],
                listeners: { show: function () {
                    const values = this.up('window').getValues();
                    const data = [{ key: 'Node', value: values.nodename }, { key: 'DCT ID', value: values.vmid || 'Next available guest ID' }, { key: 'Project', value: values.name },
                        { key: 'Deployment', value: values.mode === 'compose' ? 'Docker Compose' : values.image },
                        { key: 'Bridge', value: values.bridge }, { key: 'IPv4', value: values.ipv4mode === 'dhcp' ? 'Assigned by DHCP' : values.address },
                        { key: 'MAC address', value: values.hwaddr || 'Auto-generated' },
                        { key: 'Published ports', value: values.mode === 'compose' ? translate('Read from Compose YAML') : values.ports || translate('None') }];
                    this.down('grid').getStore().loadData(data);
                    this.up('window').validateDeployment();
                } },
                onSubmit: function () {
                    const wizard = this.up('window');
                    const values = wizard.getValues();
                    if (wizard.validatedDeployment !== JSON.stringify(deployment(values))) {
                        wizard.validateDeployment(); return;
                    }
                    request({ url: '/nodes/' + encodeURIComponent(values.nodename) + '/docker' + (wizard.projectId ? '/' + wizard.projectId : ''),
                        method: wizard.projectId ? 'PUT' : 'POST',
                        params: { deployment: JSON.stringify(deployment(values)) }, waitMsgTarget: wizard,
                        success: response => { task(response); wizard.close(); } });
                },
            }];
            me.validateDeployment = () => {
                const values = me.getValues();
                const payload = JSON.stringify(deployment(values));
                me.validatedDeployment = null;
                const submit = me.down('#submit'); if (submit) { submit.setDisabled(true); }
                request({ url: '/nodes/' + encodeURIComponent(values.nodename) + '/docker', method: 'PUT',
                    params: Object.assign({ deployment: payload }, me.projectId ? { id: me.projectId } : {}), waitMsgTarget: me,
                    success: response => {
                        if (me.destroyed || JSON.stringify(deployment(me.getValues())) !== payload) { return; }
                        me.validatedDeployment = payload;
                        const review = me.down('#reviewGrid').getStore();
                        const submit = me.down('#submit'); if (submit) { submit.setDisabled(false); }
                        review.remove(review.getRange().filter(record => record.get('key').startsWith('Service: ')));
                        response.result.data.forEach(service => review.add({ key: 'Service: ' + service.service,
                            value: [service.image, service.replicas + ' ' + translate('instances'),
                                service.ports.map(port => port.published + ':' + port.target + '/' + port.protocol).join(', '),
                                service.mounts.map(mount => mount.target).join(', ')].filter(Boolean).join(' · ') }));
                    } });
            };
            me.callParent();
            const helpButton = me.down('#help');
            if (helpButton) { helpButton.setHandler(help); }
            [['next', 'Next'], ['back', 'Back'], ['submit', me.projectId ? 'Apply' : 'Deploy']].forEach(([id, text]) => {
                const button = me.down('#' + id);
                if (button) { button.setText(translate(text)); }
            });
        },
    });

    Ext.define('PVE.betterdocker.Console', {
        extend: 'Ext.panel.Panel', alias: 'widget.pveBetterDockerConsole', layout: 'fit', bodyStyle: 'background:#000',
        initComponent: function () {
            const me = this;
            const disconnect = () => {
                if (me.socket) { me.socket.close(); me.socket = null; }
                clearInterval(me.ping);
                if (me.terminal) { me.terminal.dispose(); me.terminal = null; }
                me.connected = false; me.fit = null;
                me.generation = (me.generation || 0) + 1;
            };
            const resize = () => { if (me.fit) { me.fit.fit(); } };
            const connect = () => {
                const container = me.down('#consoleContainer').getValue();
                if (!container) { Ext.Msg.alert(translate('Console'), translate('Choose a running container.')); return; }
                disconnect();
                const generation = me.generation;
                request({ url: me.apiUrl + '/termproxy', method: 'POST',
                    params: { container, shell: me.down('#shell').getValue() }, waitMsgTarget: me,
                    success: response => {
                        if (me.destroyed || generation !== me.generation) { return; }
                        const data = response.result.data;
                        me.terminal = new Terminal({ cursorBlink: true, fontSize: 14, scrollback: 5000 });
                        me.fit = new FitAddon.FitAddon();
                        me.terminal.loadAddon(me.fit);
                        me.terminal.open(me.down('#terminal').getEl().dom);
                        resize();
                        const socket = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host +
                            '/api2/json' + me.apiUrl + '/vncwebsocket?port=' + data.port + '&vncticket=' + encodeURIComponent(data.ticket));
                        me.socket = socket;
                        socket.binaryType = 'arraybuffer';
                        socket.onopen = () => socket.send(data.user + ':' + data.ticket + '\n');
                        socket.onmessage = event => {
                            if (me.socket !== socket || !me.terminal) { return; }
                            const bytes = new Uint8Array(event.data);
                            if (!me.connected) {
                                if (bytes[0] !== 79 || bytes[1] !== 75) { socket.close(); return; }
                                me.connected = true;
                                me.terminal.write(bytes.slice(2));
                                me.terminal.focus();
                                socket.send('1:' + me.terminal.cols + ':' + me.terminal.rows + ':');
                            } else { me.terminal.write(bytes); }
                        };
                        socket.onclose = () => {
                            if (me.socket === socket && me.terminal) {
                                me.connected = false; clearInterval(me.ping);
                                me.terminal.write('\r\nConnection closed. Use Connect to open a new session.\r\n');
                            }
                        };
                        socket.onerror = () => { if (me.terminal) { me.terminal.write('\r\nConsole connection failed.\r\n'); } };
                        me.terminal.onData(value => {
                            if (me.connected) { socket.send('0:' + new TextEncoder().encode(value).length + ':' + value); }
                        });
                        me.terminal.onResize(size => { if (me.connected) { socket.send('1:' + size.cols + ':' + size.rows + ':'); } });
                        me.ping = setInterval(() => { if (socket.readyState === WebSocket.OPEN) { socket.send('2'); } }, 30000);
                    },
                });
            };
            me.tbar = [
                { xtype: 'combobox', itemId: 'consoleContainer', fieldLabel: translate('Container'), labelWidth: 65, width: 350,
                    queryMode: 'local', editable: false, displayField: 'name', valueField: 'id',
                    store: { fields: ['id', 'name'], data: [] } },
                { xtype: 'combobox', itemId: 'shell', width: 110, value: '/bin/sh', editable: false,
                    queryMode: 'local', store: [['/bin/sh', '/bin/sh'], ['/bin/bash', '/bin/bash']] },
                { text: translate('Connect'), handler: connect }, { text: translate('Disconnect'), handler: disconnect },
            ];
            me.items = [{ xtype: 'component', itemId: 'terminal' }];
            me.callParent();
            const targets = () => request({ url: me.apiUrl + '/management', method: 'GET', success: response => {
                if (me.destroyed) { return; }
                const combo = me.down('#consoleContainer'), selected = combo.getValue();
                const containers = response.result.data.containers.filter(container => container.state === 'running');
                combo.getStore().loadData(containers);
                combo.setValue(containers.some(container => container.id === selected) ? selected : (containers[0] || {}).id);
            } });
            me.on('afterrender', targets, me, { single: true });
            me.on('activate', targets);
            me.on('resize', resize);
            me.on('beforedestroy', disconnect);
        },
    });

    Ext.define('PVE.betterdocker.Notes', {
        extend: 'Proxmox.panel.NotesView', alias: 'widget.pveBetterDockerNotes',
        maxLength: 8192,
        cbindData: function () {
            this.url = '/api2/extjs' + this.apiUrl + '/config';
            this.pveType = 'docker';
            this.load();
            return {};
        },
    });

    Ext.define('PVE.betterdocker.Summary', {
        extend: 'Ext.panel.Panel', alias: 'widget.pveBetterDockerSummary', scrollable: true, bodyPadding: 5,
        updateSnapshot: function (data) {
            const me = this, metrics = data.metrics;
            const update = (id, text, usage) => me.down('#' + id).updateValue(text, usage);
            me.down('#statusPanel').setTitle(Ext.htmlEncode(data.name) + ' (' + translate('Uptime') + ': ' +
                Proxmox.Utils.format_duration_long(metrics.uptime) + ')');
            update('statusValue', Ext.htmlEncode(statusText(data.status)));
            update('nodeValue', Ext.htmlEncode(data.node));
            update('serviceValue', data.containers.filter(item => item.state === 'running').length + ' ' + translate('running') + ' / ' +
                data.containers.length + ' ' + translate('containers') + ' · ' + data.services.length + ' ' + translate('services'));
            update('cpuValue', Proxmox.Utils.render_cpu_usage(metrics.cpu, metrics.cpus), Math.min(1, metrics.cpu));
            update('memoryValue', metrics.maxmem ? Proxmox.Utils.render_size_usage(metrics.mem, metrics.maxmem) : Proxmox.Utils.format_size(metrics.mem),
                metrics.maxmem ? metrics.mem / metrics.maxmem : 0);
            me.down('#memoryValue').setPrintBar(metrics.maxmem > 0);
            ['swap', 'disk'].forEach(key => {
                const used = metrics[key], maximum = metrics['max' + key];
                const available = typeof used === 'number' && typeof maximum === 'number';
                const text = !available ? translate('Unavailable') : maximum > 0 ? Proxmox.Utils.render_size_usage(used, maximum) :
                    Proxmox.Utils.format_size(used);
                update(key + 'Value', text, available && maximum > 0 ? Math.min(1, used / maximum) : 0);
                me.down('#' + key + 'Value').setPrintBar(available && maximum > 0);
            });
            update('ipValue', Ext.htmlEncode(data.address || translate('Waiting for DHCP')));
        },
        initComponent: function () {
            const me = this;
            const store = Ext.create('Proxmox.data.RRDStore', {
                rrdurl: '/api2/json' + me.apiUrl + '/rrddata', model: 'pve-rrd-guest',
            });
            const metric = (id, title, icon, bar) => ({ xtype: 'pmxInfoWidget', itemId: id, title: translate(title),
                iconCls: icon, printBar: Boolean(bar), padding: '2 25', text: translate('Loading...') });
            const charts = [
                ['CPU Usage', ['cpu'], ['CPU usage'], 'percent'],
                ['Memory Usage', ['maxmem', 'mem'], ['Total', 'Used'], 'bytes'],
                ['Network Traffic', ['netin', 'netout'], ['Incoming', 'Outgoing'], 'bytes'],
                ['Disk IO', ['diskread', 'diskwrite'], ['Reads', 'Writes'], 'bytes'],
            ].map(([title, fields, fieldTitles, unit]) => ({ xtype: 'proxmoxRRDChart', title: translate(title),
                pveSelNode: me.pveSelNode, store, fields, fieldTitles: fieldTitles.map(translate), unit,
                powerOfTwo: title === 'Memory Usage' }));
            me.tbar = ['->', { xtype: 'proxmoxRRDTypeSelector' }];
            me.items = [{ xtype: 'container', itemId: 'itemcontainer', minWidth: 700, layout: 'column',
                defaults: { minHeight: 350, padding: 5 }, items: [
                    { xtype: 'container', height: 360, layout: { type: 'hbox', align: 'stretch' }, items: [
                        { xtype: 'panel', itemId: 'statusPanel', title: me.projectName, flex: 1, padding: '0 5 0 0',
                            layout: { type: 'vbox', align: 'stretch' }, items: [
                                { xtype: 'box', height: 20 },
                                metric('statusValue', 'Status', 'fa fa-info fa-fw'),
                                metric('nodeValue', 'Node', 'fa fa-building fa-fw'),
                                metric('serviceValue', 'Services', 'fa fa-cubes fa-fw'),
                                { xtype: 'box', height: 10 },
                                metric('cpuValue', 'CPU usage', 'fa fa-fw pmx-itype-icon-processor pmx-icon', true),
                                metric('memoryValue', 'Memory usage', 'fa fa-fw pmx-itype-icon-memory pmx-icon', true),
                                metric('swapValue', 'SWAP usage', 'fa fa-refresh fa-fw', true),
                                Object.assign(metric('diskValue', 'Container root filesystems', 'fa fa-hdd-o fa-fw', true), {
                                    listeners: { afterrender: widget => { widget.diskTooltip = Ext.create('Ext.tip.ToolTip', { target: widget.getEl(),
                                        html: 'Total size of container root filesystems, including images and writable layers. Capacity is shared Docker storage. Volumes and bind mounts are excluded; shared image blocks may be counted for each container.',
                                    }); }, destroy: widget => {
                                            if (widget.diskTooltip) { widget.diskTooltip.destroy(); }
                                        } },
                                }),
                                { xtype: 'box', height: 10 },
                                metric('ipValue', 'IPs', 'fa fa-exchange fa-fw'),
                            ] },
                        { xtype: 'pveBetterDockerNotes', itemId: 'notesview', apiUrl: me.apiUrl,
                            pveSelNode: me.pveSelNode, flex: 1, padding: '0 0 0 5' },
                    ] }, ...charts,
                ], listeners: { resize: container => Proxmox.Utils.updateColumns(container) } }];
            me.callParent();
            store.startUpdate();
            me.on('destroy', () => { store.stopUpdate(); store.destroy(); });
            me.mon(Ext.state.Manager.getProvider(), 'statechange', (_, key) => {
                if (key === 'summarycolumns') { Proxmox.Utils.updateColumns(me.down('#itemcontainer')); }
            });
        },
    });

    Ext.define('PVE.betterdocker.Logs', {
        extend: 'Ext.panel.Panel', alias: 'widget.pveBetterDockerLogs', layout: 'fit',
        setTarget: function (service, container) {
            this.down('#logService').setValue(service || '');
            this.down('#logContainer').setValue(container || ''); this.reloadLogs();
        },
        initComponent: function () {
            const me = this;
            let content = '', loading = false, containers = [];
            const updateContainers = () => {
                const service = me.down('#logService').getValue();
                const combo = me.down('#logContainer'), selected = combo.getValue();
                const matching = containers.filter(container => !service || container.service === service);
                combo.getStore().loadData([{ id: '', name: translate('All instances') }, ...matching]);
                combo.setValue(matching.some(container => container.id === selected) ? selected : '');
            };
            const render = () => {
                const query = String(me.down('#logSearch').getValue() || '').toLowerCase();
                const area = me.down('#logOutput');
                const el = area.inputEl && area.inputEl.dom;
                const scroll = el ? el.scrollTop : 0;
                const follow = me.down('#logFollow').getValue();
                area.setValue(query ? content.split('\n').filter(line => line.toLowerCase().includes(query)).join('\n') : content);
                if (el) { el.scrollTop = follow ? el.scrollHeight : scroll; }
            };
            me.reloadLogs = () => {
                if (loading || me.destroyed) { return; }
                loading = true;
                const params = { tail: me.down('#logTail').getValue() || 200 };
                const service = me.down('#logService').getValue(), container = me.down('#logContainer').getValue();
                if (service) { params.service = service; }
                if (container) { params.container = container; }
                request({ url: me.apiUrl + '/logs', method: 'GET', params,
                    failure: response => { loading = false; if (!me.destroyed) { Proxmox.Utils.setErrorMask(me, response.htmlStatus); } },
                    success: response => { loading = false; if (!me.destroyed) {
                        Proxmox.Utils.setErrorMask(me, false); content = response.result.data; render();
                    } } });
            };
            const targets = () => request({ url: me.apiUrl + '/management', method: 'GET', success: response => {
                if (me.destroyed) { return; }
                me.down('#logService').getStore().loadData([{ name: '', label: translate('All services') },
                    ...response.result.data.services.map(s => ({ name: s.name, label: s.name }))]);
                containers = response.result.data.containers;
                updateContainers();
            } });
            me.tbar = [
                { xtype: 'combobox', itemId: 'logService', emptyText: translate('All services'), width: 160, editable: false, queryMode: 'local',
                    value: '', displayField: 'label', valueField: 'name', store: { fields: ['name', 'label'], data: [] },
                    listeners: { select: () => { updateContainers(); me.reloadLogs(); } } },
                { xtype: 'combobox', itemId: 'logContainer', emptyText: translate('All instances'), width: 180, editable: false, queryMode: 'local',
                    value: '', displayField: 'name', valueField: 'id', store: { fields: ['id', 'name'], data: [] },
                    listeners: { select: me.reloadLogs } },
                { xtype: 'numberfield', itemId: 'logTail', fieldLabel: translate('Lines'), labelWidth: 40, width: 120,
                    minValue: 1, maxValue: 5000, value: 200, allowDecimals: false },
                { text: translate('Refresh'), handler: me.reloadLogs },
                { xtype: 'checkbox', itemId: 'logAuto', boxLabel: translate('Live'), checked: true },
                { xtype: 'checkbox', itemId: 'logFollow', boxLabel: translate('Follow'), checked: true },
                '->', { text: translate('Download'), handler: () => download('docker.log', content) },
            ];
            me.items = [{ xtype: 'textarea', itemId: 'logOutput', readOnly: true, fieldCls: 'bd-source' }];
            me.bbar = [{ xtype: 'textfield', itemId: 'logSearch', fieldLabel: translate('Search'), labelWidth: 55,
                width: 400, enableKeyEvents: true, listeners: { change: render } }];
            me.callParent();
            me.on('afterrender', () => { targets(); me.reloadLogs(); me.logPoll = setInterval(() => {
                if (!me.destroyed && me.isVisible(true) && me.down('#logAuto').getValue()) { me.reloadLogs(); }
            }, 3000); }, me, { single: true });
            me.on('activate', () => { targets(); me.reloadLogs(); });
            me.on('beforedestroy', () => clearInterval(me.logPoll));
        },
    });

    Ext.define('PVE.betterdocker.Diagnostics', {
        extend: 'Ext.grid.Panel', alias: 'widget.pveBetterDockerDiagnostics', hideHeaders: true,
        initComponent: function () {
            const me = this;
            me.store = { fields: ['key', 'value'], data: [] };
            me.columns = [{ text: translate('Name'), dataIndex: 'key', width: 230, renderer: value => Ext.htmlEncode(translate(value)) },
                { text: translate('Value'), dataIndex: 'value', flex: 1, renderer: Ext.htmlEncode }];
            const reload = () => request({ url: '/nodes/' + encodeURIComponent(me.pveSelNode.data.node) + '/docker', method: 'GET', params: { diagnostics: 1 },
                failure: response => { if (!me.destroyed) { Proxmox.Utils.setErrorMask(me, response.htmlStatus); } },
                success: response => {
                    if (me.destroyed) { return; }
                    Proxmox.Utils.setErrorMask(me, false);
                    const info = response.result.data[0];
                    me.getStore().loadData([
                        { key: 'BetterDockers version', value: info.addon }, { key: 'Proxmox version', value: info.pve },
                        { key: 'Docker Engine', value: info.engine }, { key: 'Docker Compose', value: info.compose },
                        { key: 'Integration', value: info.integration }, { key: 'Maintenance timer', value: info.timer },
                        { key: 'Network restoration', value: info.addresses }, { key: 'Update integration check', value: info.integration_check }, { key: 'Projects', value: info.projects },
                        { key: 'Docker storage', value: info.storage.path || translate('Unavailable') },
                        { key: 'Storage usage', value: info.storage.total ? Proxmox.Utils.render_size_usage(info.storage.used, info.storage.total) : translate('Unavailable') },
                        ...info.dhcp.map(item => ({ key: 'DHCP — DCT ' + item.vmid, value: item.state })),
                    ]);
                } });
            me.tbar = [{ text: translate('Refresh'), handler: reload }, '->', { text: translate('Help'), handler: help }];
            me.callParent(); me.on('activate', reload); me.on('afterrender', reload, me, { single: true });
        },
    });
    if (PVE.node.Config) {
        const nodeInit = PVE.node.Config.prototype.initComponent;
        Ext.override(PVE.node.Config, {
            initComponent: function () {
                nodeInit.apply(this, arguments);
                this.insertNodes([{ xtype: 'pveBetterDockerDiagnostics', title: 'Docker', itemId: 'betterdocker',
                    iconCls: 'fa bd-docker-icon', pveSelNode: this.pveSelNode }]);
            },
        });
    }

    Ext.define('PVE.betterdocker.DeploymentEdit', {
        extend: 'Proxmox.window.Edit', width: 600, resizable: false, constrain: true, onlineHelp: 'chapter_system_administration',
        initComponent: function () {
            const me = this, defaults = me.deployment;
            if (typeof window.innerHeight === 'number') { if (typeof me.height === 'number') { me.height = Math.min(me.height, window.innerHeight - 40); } me.width = Math.min(me.width, window.innerWidth - 40); }
            me.title = translate('Edit deployment') + ': DCT ' + me.vmid;
            me.url = '/nodes/' + encodeURIComponent(me.nodename) + '/docker/' + me.projectId;
            me.items = [{ xtype: 'inputpanel', items: [
                field('mode', 'Deploy from', defaults.mode, { xtype: 'combobox', editable: false, queryMode: 'local',
                    store: [['compose', 'Docker Compose'], ['image', 'Docker image']], listeners: { change: updateMode, afterrender: updateMode } }),
                { xtype: 'container', itemId: 'composeFields', layout: 'anchor', items: [
                    field('compose', 'Compose YAML', defaults.compose || '', { xtype: 'pveBetterDockerCodeArea', height: 230, allowBlank: false }),
                    importRow('Load YAML', 'compose', 240000)] },
                { xtype: 'container', itemId: 'imageFields', layout: 'anchor', items: [field('image', 'Image', defaults.image || ''),
                    field('ports', 'Published ports', defaults.ports || '', { xtype: 'textarea', height: 120 })] },
            ], advancedItems: [
                field('environment', '.env', defaults.environment || '', { xtype: 'textarea', height: 65, fieldCls: 'bd-source' }),
                importRow('Load .env', 'environment', 16000),
            ] }];
            me.submit = () => {
                if (!me.formPanel.getForm().isValid()) { return; }
                const changed = me.getValues();
                const payload = JSON.stringify(deployment(Object.assign({}, defaults, changed, { vmid: me.vmid })));
                request({ url: '/nodes/' + encodeURIComponent(me.nodename) + '/docker', method: 'PUT',
                    params: { id: me.projectId, deployment: payload }, waitMsgTarget: me,
                    success: () => request({ url: me.url, method: 'PUT', params: { deployment: payload }, waitMsgTarget: me,
                        success: response => { trackTask(response, me.ownerPanel); task(response); me.close(); } }) });
            };
            me.callParent();
            configureHelp(me);
        },
    });

    Ext.define('PVE.betterdocker.SnapshotEdit', {
        extend: 'PVE.window.Snapshot',
        submit: function () {
            const me = this, form = me.formPanel.getForm();
            if (me.viewonly || !form.isValid()) { return; }
            const values = form.getValues();
            request({ url: me.url, method: me.isCreate ? 'POST' : 'PUT', waitMsgTarget: me,
                params: me.isCreate ? { action: 'create', snapname: values.snapname, description: values.description || '' } :
                    { description: values.description || '' },
                success: response => {
                    if (me.isCreate) { trackTask(response, me.ownerPanel); task(response); }
                    me.close();
                } });
        },
        initComponent: function () {
            const me = this;
            // Native Snapshot uses vmid in its API URL; DCT routes use the private project ID.
            me.vmid = me.projectId;
            me.type = 'docker';
            me.callParent();
            me.setTitle(me.isCreate ? translate('Create') + ': DCT ' + me.guestId + ' (' + me.vmname + ') ' + translate('Snapshot') :
                translate(me.viewonly ? 'View' : 'Edit') + ': ' + translate('Snapshot') + ' ' + me.snapname);
            const description = me.down('[name=description]');
            if (description) { description.setReadOnly(Boolean(me.viewonly)); description.maxLength = 4096; }
            const name = me.down('[name=snapname]');
            if (me.isCreate && name) { name.maxLength = 40; name.enforceMaxLength = true; }
            const summary = me.down('#summary');
            if (summary) { summary.getColumns().forEach(column => { column.renderer = Ext.htmlEncode; }); }
        },
    });

    Ext.define('PVE.betterdocker.Snapshots', {
        extend: 'PVE.guest.SnapshotTree', alias: 'widget.pveBetterDockerSnapshots', type: 'docker',
        // Reuse the native LXC tree, columns, toolbar, keyboard handling and editor.
        controller: {
            xclass: 'Ext.app.ViewController',
            init: function (view) {
                const me = this, vm = me.getViewModel(), info = view.ownerPanel.pveSelNode.data;
                vm.set('type', 'docker'); vm.set('nodename', info.node); vm.set('vmid', info.vmid); vm.set('vmname', info.name);
                me.load_task = new Ext.util.DelayedTask(me.reload, me);
                view.getStore().sorters.add({ property: 'order', direction: 'ASC' });
                view.updateActions();
                me.reload();
            },
            cancel: function () { this.load_task.cancel(); },
            select: function (_, records) {
                this.getViewModel().set('selected', records.length ? records[0].get('name') : '');
            },
            newSnapshot: function () { this.run_editor(false); },
            editSnapshot: function () { this.run_editor(true); },
            run_editor: function (edit) {
                const me = this, view = me.getView(), vm = me.getViewModel(), snapname = vm.get('selected');
                if (edit && (!snapname || snapname === 'current')) { return; }
                const info = view.ownerPanel.pveSelNode.data;
                const win = Ext.create('PVE.betterdocker.SnapshotEdit', {
                    ownerPanel: view.ownerPanel, nodename: info.node, projectId: (info.realId || info.id).split('/')[1], guestId: info.vmid, vmname: info.name,
                    isCreate: !edit, viewonly: !vm.get('snapshotAllowed'), snapname: edit ? snapname : undefined,
                    submitText: !edit ? translate('Take Snapshot') : undefined,
                });
                win.show();
                me.mon(win, 'destroy', () => { me.reload(); view.ownerPanel.reloadDocker(); }, me);
            },
            rollback: function () { this.snapshotAction('restore'); },
            remove: function () { this.snapshotAction('delete'); },
            snapshotAction: function (action) {
                const me = this, view = me.getView(), snapname = me.getViewModel().get('selected');
                if (!snapname || snapname === 'current') { return; }
                request({ url: view.apiUrl + '/snapshot', method: 'POST', params: { action, snapname }, waitMsgTarget: view,
                    callback: () => me.reload(), success: response => {
                        trackTask(response, view.ownerPanel);
                        Ext.create('Proxmox.window.TaskProgress', { upid: response.result.data, autoShow: true });
                    } });
            },
            reload: function () {
                const me = this, view = me.getView();
                if (me.destroyed || view.destroyed || me.loadingSnapshots) { return; }
                me.loadingSnapshots = true;
                request({ url: view.apiUrl + '/snapshots', method: 'GET', success: response => {
                    me.loadingSnapshots = false;
                    if (me.destroyed || view.destroyed) { return; }
                    Proxmox.Utils.setErrorMask(view, false);
                    const vm = me.getViewModel(), rows = response.result.data, current = rows.find(row => row.name === 'current');
                    vm.set('running', Boolean(current?.running));
                    if (current?.digest !== me.old_digest) {
                        me.old_digest = current?.digest;
                        const selected = vm.get('selected'), nodes = {};
                        rows.forEach(row => { nodes[row.name] = Object.assign({}, row, { children: [], leaf: true,
                            iconCls: row.name === 'current' ? 'fa fa-cube fa-fw x-fa-tree' : 'fa fa-fw fa-history x-fa-tree' }); });
                        const root = { name: '__root', expanded: true, children: [] };
                        rows.forEach(row => {
                            const item = nodes[row.name], parent = nodes[row.parent];
                            if (parent) { parent.children.push(item); parent.leaf = false; parent.expanded = true; parent.expandable = false; }
                            else { root.children.push(item); }
                        });
                        view.setRootNode(root);
                        if (selected) {
                            const record = view.getRootNode().findChild('name', selected, true);
                            if (record) { view.getSelectionModel().select(record); }
                        }
                    }
                    view.updateActions(); me.load_task.delay(3000);
                }, failure: response => {
                    me.loadingSnapshots = false;
                    if (!me.destroyed && !view.destroyed) { Proxmox.Utils.setErrorMask(view, response.htmlStatus); me.load_task.delay(3000); }
                } });
            },
        },
        updateActions: function () {
            const me = this, vm = me.getViewModel(), owner = me.ownerPanel, data = owner.snapshotState;
            const unlocked = Boolean(data && !data.operation?.active && !owner.pendingTask);
            const supported = Boolean(data?.containers?.length && data.containers.length === data.expected && data.containers.every(item => ['created', 'exited', 'running'].includes(item.state)));
            vm.set('snapshotAllowed', unlocked && Boolean(data?.capabilities?.snapshot));
            vm.set('rollbackAllowed', unlocked && Boolean(data?.capabilities?.rollback));
            vm.set('snapshotFeature', supported);
        },
        loadSnapshots: function () { this.getController().reload(); },
        initComponent: function () {
            const me = this;
            me.callParent();
            // The native rollback confirmation uses a VM identifier; retain its wording for DCT.
            const button = me.query('proxmoxButton').find(item => item.text === translate('Rollback'));
            if (button) { button.confirmMsg = function () {
                const info = me.ownerPanel.pveSelNode.data, record = me.getSelectionModel().getSelection()[0];
                return Ext.htmlEncode(translate('Rollback') + ' DCT ' + info.vmid + ' (' + info.name + ") '" + record.get('name') + "'? " + translate('Current state will be lost.'));
            }; }
        },
    });

    Ext.define('PVE.betterdocker.Config', {
        extend: 'PVE.panel.Config', alias: 'widget.pveBetterDockerConfig', showSearch: false,
        // The native Config panel passes this to Help on every card activation.
        onlineHelp: 'chapter_system_administration',
        activateCard: function (cardid) {
            try {
                return PVE.panel.Config.prototype.activateCard.call(this, cardid);
            } finally {
                // Native selectionchange resets this only after activateCard returns.
                this.suspendLayout = false;
            }
        },
        initComponent: function () {
            const me = this;
            const info = me.pveSelNode.data;
            const identifier = (info.realId || info.id).split('/')[1];
            const url = '/nodes/' + encodeURIComponent(info.node) + '/docker/' + identifier;
            let current;
            let selectedService;
            let loading = false;
            const editable = () => current && current.capabilities ? Boolean(current.capabilities.configure) : Proxmox.UserName === 'root@pam';
            const locked = () => Boolean(me.pendingTask || current && current.operation && current.operation.active);
            const set = (id, value) => { const item = me.down('#' + id); if (item) { item.setValue(value); } };
            const rows = (id, data) => {
                const item = me.down('#' + id); if (!item) { return; }
                const model = item.getSelectionModel && item.getSelectionModel();
                const selected = model && model.getSelection()[0];
                const key = selected && (selected.get('id') || selected.get('key'));
                item.getStore().loadData(data);
                if (key && model) { const record = item.getStore().getRange().find(row => (row.get('id') || row.get('key')) === key); if (record) { model.select(record); } }
            };
            const service = () => current && current.services.find(item => item.name === selectedService);
            const populate = () => {
                if (!current) { return; }
                const running = current.containers.some(item => item.state === 'running');
                [['startButton', current.status === 'running'], ['stopButton', !running], ['restartButton', !running]].forEach(([id, disabled]) => {
                    const button = me.down('#' + id); if (button) { button.setDisabled(disabled || locked() || current.capabilities && !current.capabilities.power); }
                });
                const consoleButton = me.down('#consoleButton');
                if (consoleButton) { consoleButton.setDisabled(!running || current.capabilities && !current.capabilities.console); }
                me.query('[bdWrite=true]').forEach(button => button.setDisabled(locked() || !editable()));
                const op = current.operation || {};
                const operationLabel = me.down('#operationLabel');
                if (operationLabel) { operationLabel.setText(op.active || me.pendingTask ? '<i class="fa fa-lock"></i> ' + Ext.htmlEncode(translate('Operation in progress')) : op.failed ? Ext.htmlEncode(translate(op.message)) : ''); }
                me.snapshotState = current;
                const snapshots = me.down('#snapshots');
                if (snapshots && snapshots.rendered) { snapshots.updateActions(); if (!snapshots.isHidden()) { snapshots.loadSnapshots(); } }
                const tags = me.down('#dockerTags');
                if (tags) { tags.loadTags(current.tags || ''); tags.canEdit = editable(); if (tags.getViewModel) { tags.getViewModel().set('canEdit', editable() && !locked()); } }
                rows('serviceGrid', current.containers.concat(current.services.filter(service => !current.containers.some(container => container.service === service.name)).map(service => ({
                    id: '', name: '—', service: service.name, image: service.image, state: 'stopped', health: 'none', restarts: 0, exitcode: '—',
                }))).map(container => Object.assign({}, container, { ports: (current.services.find(service => service.name === container.service)?.ports || [])
                    .map(port => port.published + ':' + port.target + '/' + port.protocol).join(', ') })));
                selectedService = service() ? selectedService : (current.services[0] || {}).name;
                me.query('[itemId=serviceSelector]').forEach(combo => {
                    combo.getStore().loadData(current.services.map(item => ({ name: item.name })));
                    combo.setValue(selectedService);
                    combo.setHidden(current.services.length < 2);
                });
                const summary = me.down('#summary');
                if (summary) { summary.updateSnapshot(current); }
                rows('networkGrid', [{ name: 'net0', bridge: current.bridge, hwaddr: current.hwaddr || '',
                    mtu: current.mtu || 'Same as bridge', address: current.address || 'Waiting for DHCP',
                    mode: current.ipv4mode === 'dhcp' ? 'DHCP' : 'Static' }]);
                rows('portGrid', current.services.flatMap(item => item.ports.map(port => Object.assign({ service: item.name }, port))));
                const active = service();
                if (active) {
                    const values = active.settings;
                    const restore = me.down('#restoreCompose'); if (restore) { restore.setDisabled(!active.overridden || !editable() || locked()); }
                    rows('resourceGrid', [
                        { key: 'Memory', value: values.memory ? values.memory + ' MiB' : 'Unlimited' },
                        { key: 'Swap', value: values.memory ? values.swap === -1 ? translate('Unlimited') : values.swap + ' MiB' : 'Docker default' },
                        { key: 'CPU limit', value: values.cpus || 'Unlimited' },
                        { key: 'CPU units', value: values.cpuunits || 'Docker default' },
                        { key: 'PID limit', value: values.pids || 'Unlimited' },
                        { key: 'Container disk limit', value: values.disk ? values.disk + ' MiB' : (current.storage?.rootfs?.supported ? 'Unlimited' : 'Not supported by host') },
                        { key: 'Project data disk', value: current.storage?.limit_mib ? current.storage.limit_mib + ' MiB' + (current.storage.mounted ? ' (' + Proxmox.Utils.format_size(current.storage.used) + ' / ' + Proxmox.Utils.format_size(current.storage.available) + ' ' + translate('Available') + ')' : ' — ' + translate('Unavailable')) : 'Unlimited' },
                        { key: 'Settings source', value: active.overridden ? 'GUI override' : 'Compose' },
                    ].concat(active.mounts.map((mount, index) => ({ key: 'Mount Point (' + index + ')', value: (mount.source || '') + ',mp=' + mount.target + (mount.read_only ? ',ro=1' : '') }))));
                    rows('dnsGrid', [{ key: 'DNS servers', value: values.dns.join(', ') || 'Docker default' },
                        { key: 'Search domains', value: values.dns_search.join(', ') || 'Docker default' }]);
                    rows('optionGrid', [{ key: 'Restart policy', value: values.restart }, { key: 'Image', value: active.image }]);
                }
                if (current.deployment) { set('source', current.deployment.mode === 'compose' ? current.deployment.compose :
                    'Image: ' + current.deployment.image + '\nPublished ports:\n' + (current.deployment.ports || 'None')); }
                if (me.updateServiceActions) { const grid = me.down('#serviceGrid'); if (grid) { me.updateServiceActions(grid.getSelectionModel().getSelection()); } }
                const consoleCombo = me.down('#consoleContainer');
                if (consoleCombo) {
                    const old = consoleCombo.getValue();
                    const containers = current.containers.filter(item => item.state === 'running');
                    consoleCombo.getStore().loadData(containers);
                    consoleCombo.setValue(containers.some(item => item.id === old) ? old : (containers[0] || {}).id);
                }
            };
            const reload = () => {
                if (loading || me.destroyed) { return; }
                loading = true;
                request({ url: url + '/management', method: 'GET',
                    success: response => { loading = false; if (!me.destroyed) { Proxmox.Utils.setErrorMask(me, false); current = response.result.data; populate(); } },
                    failure: response => { loading = false; if (!me.destroyed) { Proxmox.Utils.setErrorMask(me, response.htmlStatus); } },
                });
            };
            me.reloadDocker = reload;
            const act = operation => { if (!locked()) { dockerAction(info, operation, me); } };
            const editDeployment = () => request({ url, method: 'GET', success: response => {
                Ext.create('PVE.betterdocker.DeploymentEdit', { ownerPanel: me, projectId: identifier, vmid: info.vmid, nodename: info.node,
                    deployment: response.result.data.deployment }).show();
            } });
            const editDataDisk = () => {
                if (!editable() || locked() || !current) { return; }
                const existing = current.storage?.limit_mib || 0;
                const win = Ext.create('Proxmox.window.Edit', {
                    title: translate('Edit data disk'), url: url + '/storage-limit', method: 'PUT', width: 400,
                    onlineHelp: 'chapter_system_administration', listeners: { afterrender: configureHelp },
                    items: [{ xtype: 'inputpanel', items: [field('size_mib', 'Data disk (MiB)', existing || 1024,
                        { xtype: 'numberfield', minValue: existing ? existing + 1 : 256, maxValue: 1048576, allowDecimals: false, allowBlank: false })] }],
                    submit: function () {
                        const form = this.formPanel.getForm();
                        if (!form.isValid()) { return; }
                        request({ url: url + '/storage-limit', method: 'PUT', waitMsgTarget: win, params: form.getValues(),
                            success: response => { trackTask(response, me); task(response); win.close(); } });
                    },
                });
                win.show();
            };
            const editSettings = (group, selectedKey) => {
                const active = service();
                if (!active || !editable() || locked()) { return; }
                if (selectedKey === 'Settings source') { return; }
                if (selectedKey === 'Project data disk') { editDataDisk(); return; }
                if (selectedKey === 'Image') { editDeployment(); return; }
                if (selectedKey && selectedKey.startsWith('Mount Point')) { editDeployment(); return; }
                const values = active.settings;
                const numeric = (name, label, maximum, extra) => field(name, label, values[name] || 0,
                    Object.assign({ xtype: 'numberfield', minValue: 0, maxValue: maximum, allowDecimals: false, allowBlank: false }, extra));
                let fields = group === 'resources' ? [
                    numeric('memory', 'Memory (MiB)', 1048576), numeric('swap', 'Swap (MiB)', 1048576, { minValue: -1 }),
                    numeric('cpus', 'CPU limit', 8192, { allowDecimals: true, decimalPrecision: 3 }),
                    numeric('disk', 'Container disk (MiB)', 1048576, { disabled: !current.storage?.rootfs?.supported }),
                    numeric('cpuunits', 'CPU units', 262144), numeric('pids', 'PID limit', 4194304),
                ] : group === 'dns' ? [
                    field('dns', 'DNS servers', values.dns.join(', ')), field('dns_search', 'Search domains', values.dns_search.join(', ')),
                ] : [field('restart', 'Restart policy', values.restart, { xtype: 'combobox', editable: false, queryMode: 'local',
                    store: [['no', translate('No')], ['always', translate('Always')], ['unless-stopped', translate('Unless stopped')], ['on-failure', translate('On failure')]] })];
                if (group === 'resources' && selectedKey) {
                    const names = selectedKey === 'Memory' || selectedKey === 'Swap' ? ['memory', 'swap'] :
                        selectedKey === 'PID limit' ? ['pids'] : selectedKey === 'Container disk limit' ? ['disk'] : ['cpus', 'cpuunits'];
                    fields = fields.filter(item => !item.name || names.includes(item.name));
                }
                const win = Ext.create('Proxmox.window.Edit', { title: translate('Edit') + ': ' + translate({ resources: 'Resources', dns: 'DNS', options: 'Options' }[group]) + ' - ' + active.name,
                    url: url + '/settings', method: 'PUT', width: 400, showReset: true,
                    onlineHelp: 'chapter_system_administration', listeners: { afterrender: configureHelp },
                    items: [{ xtype: 'inputpanel', items: fields }],
                    submit: function () {
                        const form = this.formPanel.getForm();
                        if (!form.isValid()) { return; }
                        const changed = form.getValues();
                        const settings = Object.assign({}, values);
                        Object.keys(changed).forEach(key => { settings[key] = group === 'resources' ? Number(changed[key]) : changed[key]; });
                        if (group === 'dns') {
                            ['dns', 'dns_search'].forEach(key => { settings[key] = changed[key].split(',').map(v => v.trim()).filter(Boolean); });
                        }
                        request({ url: url + '/settings', method: 'PUT', waitMsgTarget: win,
                            params: { service: active.name, settings: JSON.stringify(settings) },
                            success: response => { trackTask(response, me); task(response); win.close(); } });
                    },
                });
                win.show();
            };
            const selector = () => ({ xtype: 'combobox', itemId: 'serviceSelector', fieldLabel: translate('Service'), labelWidth: 50,
                queryMode: 'local', editable: false, displayField: 'name', valueField: 'name',
                store: { fields: ['name'], data: [] }, listeners: { select: combo => { selectedService = combo.getValue(); populate(); } } });
            const grid = (id, columns, fields) => ({ xtype: 'grid', itemId: id, minHeight: 150,
                store: { fields, data: [] }, columns: columns.map(column => Object.assign({ flex: 1, renderer: Ext.htmlEncode }, column)) });
            const properties = id => Object.assign(grid(id, [{ text: translate('Name'), dataIndex: 'key', width: 200, flex: 0,
                renderer: (value, meta) => {
                    const icons = { Memory: 'pmx-itype-icon-memory', Swap: 'fa fa-refresh', 'CPU limit': 'pmx-itype-icon-processor',
                        'CPU units': 'pmx-itype-icon-processor', 'PID limit': 'fa fa-cubes' };
                    const icon = icons[value] || (String(value).startsWith('Mount Point') ? 'fa fa-hdd-o' : 'fa fa-cog');
                    meta.tdCls = 'pve-itype-fa';
                    return '<i class="pve-grid-fa fa-fw ' + icon + '"></i> ' + Ext.htmlEncode(translate(value));
                } }, { text: translate('Value'), dataIndex: 'value', renderer: value => Ext.htmlEncode(translate(value)) }], ['key', 'value']), { hideHeaders: true });
            const card = (title, id, icon, items, tbar, extra) => Object.assign({ xtype: 'panel', title: translate(title), itemId: id,
                iconCls: 'fa fa-' + icon, layout: 'anchor', scrollable: true, bodyPadding: 0,
                defaults: { anchor: '100%' }, items, tbar,
                listeners: { afterrender: reload, activate: reload } }, extra);
            me.title = 'DCT ' + (info.vmid ? info.vmid + ' ' : '') + '(' + info.name + ') ' + translate('on node') + ' \'' + info.node + '\'';
            me.hstateid = 'betterdocker-' + identifier;
            me.tbarSpacing = false;
            me.tbar = [
                { xtype: 'tbtext', itemId: 'operationLabel', text: translate('') },
                { xtype: 'pveTagEditContainer', itemId: 'dockerTags', cls: 'proxmox-tags-full', tags: info.tags || '', canEdit: Proxmox.UserName === 'root@pam',
                    listeners: { change: function (tags) {
                        request({ url: url + '/config', method: 'PUT', params: { tags }, success: reload });
                    } } }, '->',
                { text: translate('Start'), itemId: 'startButton', iconCls: 'fa fa-play', handler: () => act('start') },
                { xtype: 'splitbutton', text: translate('Shutdown'), itemId: 'stopButton', iconCls: 'fa fa-power-off', handler: () => act('stop'),
                    menu: [{ text: translate('Restart'), itemId: 'restartButton', iconCls: 'fa fa-refresh', handler: () => act('restart') }] },
                { text: translate('Console'), itemId: 'consoleButton', iconCls: 'fa fa-terminal', handler: () => me.selectById('console') },
                { text: translate('More'), menu: [{ text: translate('Edit deployment'), bdWrite: true, iconCls: 'fa fa-fw fa-pencil', handler: editDeployment },
                    { text: translate('Refresh'), iconCls: 'fa fa-fw fa-refresh', handler: reload },
                    { text: translate('Remove'), bdWrite: true, iconCls: 'fa fa-trash', handler: () => Ext.Msg.confirm(translate('Remove Docker project'),
                        translate('Remove this project? Volumes and bind-mount data are retained.'), answer => { if (answer === 'yes') { act('delete'); } }) }] },
            ];
            me.items = [
                { xtype: 'pveBetterDockerSummary', title: translate('Summary'), itemId: 'summary', iconCls: 'fa fa-book',
                    apiUrl: url, projectName: info.name, pveSelNode: me.pveSelNode, listeners: { afterrender: reload } },
                { xtype: 'pveBetterDockerConsole', title: translate('Console'), itemId: 'console', iconCls: 'fa fa-terminal', apiUrl: url,
                    listeners: { afterrender: reload } },
                card('Services', 'services', 'cubes', [grid('serviceGrid', [
                    { text: translate('Service'), dataIndex: 'service' }, { text: translate('Instance'), dataIndex: 'name' },
                    { text: translate('Image'), dataIndex: 'image' }, { text: translate('Published ports'), dataIndex: 'ports' }, { text: translate('Status'), dataIndex: 'state', renderer: value => Ext.htmlEncode(statusText(value)) },
                    { text: translate('Health'), dataIndex: 'health', renderer: value => Ext.htmlEncode(translate(value === 'none' ? 'No healthcheck' : value)) },
                    { text: translate('Restarts'), dataIndex: 'restarts', width: 85, flex: 0 },
                    { text: translate('Exit code'), dataIndex: 'exitcode', width: 85, flex: 0 },
                ], ['id', 'name', 'service', 'image', 'ports', 'state', 'health', 'restarts', 'exitcode'])],
                    ['start', 'stop', 'restart'].map(verb => ({ text: translate({ start: 'Start', stop: 'Shutdown', restart: 'Restart' }[verb]),
                        itemId: 'service-' + verb, disabled: true, handler: () => {
                            const selected = me.down('#serviceGrid').getSelectionModel().getSelection()[0];
                            if (!selected || locked()) { return; }
                            const execute = () => request({ url: url + '/service-action', method: 'POST',
                                params: { service: selected.get('service'), action: verb }, success: response => trackTask(response, me) });
                            if (verb === 'start') { execute(); } else { Ext.Msg.confirm(translate('Confirm'),
                                Ext.htmlEncode(translate(verb === 'stop' ? 'Shutdown' : 'Restart') + ' ' + selected.get('service') + '?'), answer => { if (answer === 'yes') { execute(); } }); }
                        } })).concat([{ text: translate('Console'), handler: () => {
                            const record = me.down('#serviceGrid').getSelectionModel().getSelection()[0];
                            me.selectById('console'); const combo = me.down('#consoleContainer');
                            if (record && combo) { combo.setValue(record.get('id')); }
                        } }, { text: translate('Logs'), handler: () => {
                            const record = me.down('#serviceGrid').getSelectionModel().getSelection()[0];
                            me.selectById('logs'); const view = me.down('#logs');
                            if (record && view) { view.setTarget(record.get('service'), record.get('id')); }
                        } }]), { layout: 'fit' }),
                card('Resources', 'resources', 'cube', [properties('resourceGrid')],
                    [{ text: translate('Edit'), itemId: 'resourceEdit', disabled: true, handler: () => {
                        const grid = me.down('#resourceGrid');
                        const selected = grid.getSelectionModel().getSelection()[0];
                        if (selected) { editSettings('resources', selected.get('key')); }
                    } }, { text: translate('Restore Compose'), itemId: 'restoreCompose', bdWrite: true, handler: () => {
                        const active = service(); if (!active || !active.overridden) { return; }
                        request({ url: url + '/reset-settings', method: 'POST', params: { service: active.name },
                            success: response => { trackTask(response, me); task(response); } });
                    } }, '->', selector()], { layout: 'fit' }),
                card('Network', 'network', 'exchange', [Object.assign(grid('networkGrid', [
                    { text: translate('Name'), dataIndex: 'name', width: 80, flex: 0 }, { text: translate('Bridge'), dataIndex: 'bridge' },
                    { text: translate('MAC address'), dataIndex: 'hwaddr' }, { text: translate('IPv4'), dataIndex: 'address' },
                    { text: translate('IPv4 mode'), dataIndex: 'mode' }, { text: translate('MTU'), dataIndex: 'mtu' }],
                    ['name', 'bridge', 'hwaddr', 'address', 'mode', 'mtu']), { height: 90 }),
                    Object.assign(grid('portGrid', [{ text: translate('Service'), dataIndex: 'service' }, { text: translate('IP address'), dataIndex: 'host_ip' },
                        { text: translate('Published port'), dataIndex: 'published' }, { text: translate('Container port'), dataIndex: 'target' },
                        { text: translate('Protocol'), dataIndex: 'protocol' }], ['service', 'host_ip', 'published', 'target', 'protocol']), { title: translate('Published ports'), flex: 1 })],
                    [{ text: translate('Edit ports'), bdWrite: true, handler: editDeployment }], { layout: { type: 'vbox', align: 'stretch' } }),
                card('DNS', 'dns', 'globe', [properties('dnsGrid')], [{ text: translate('Edit'), itemId: 'dnsEdit', disabled: true, handler: () => { const row = me.down('#dnsGrid').getSelectionModel().getSelection()[0]; if (row) { editSettings('dns', row.get('key')); } } }, '->', selector()], { layout: 'fit' }),
                card('Options', 'options', 'cog', [properties('optionGrid')], [{ text: translate('Edit'), itemId: 'optionsEdit', disabled: true, handler: () => { const row = me.down('#optionGrid').getSelectionModel().getSelection()[0]; if (row) { editSettings('options', row.get('key')); } } }, '->', selector()], { layout: 'fit' }),
                card('Deployment', 'deployment', 'file-code-o', [{ xtype: 'pveBetterDockerCodeArea', itemId: 'source', readOnly: true, fieldStyle: 'font-family:monospace' }],
                    [{ text: translate('Edit'), bdWrite: true, handler: editDeployment }, { text: translate('Download'), handler: () => {
                        if (current?.deployment) { download(current.deployment.mode === 'compose' ? 'compose.yaml' : 'deployment.txt', me.down('#source').getValue()); }
                    } }], { layout: 'fit' }),
                { xtype: 'pveBetterDockerLogs', title: translate('Logs'), itemId: 'logs', iconCls: 'fa fa-file-text-o', apiUrl: url },
                { xtype: 'proxmoxNodeTasks', title: translate('Task History'), itemId: 'tasks', iconCls: 'fa fa-list-alt',
                    nodename: info.node, preFilter: { vmid: info.vmid } },
            ];
            const hostAccess = Proxmox.UserName === 'root@pam' || Boolean(Ext.state.Manager.get('GuiCap')?.nodes?.['Sys.Console']);
            if (!hostAccess) { me.items = me.items.filter(item => !['deployment', 'logs', 'console'].includes(item.itemId)); }
            const history = me.items.splice(me.items.findIndex(item => item.itemId === 'tasks'), 1)[0];
            me.items.splice(7, 0, history);
            me.items.splice(8, 0, { xtype: 'pveBetterDockerSnapshots', title: translate('Snapshots'), itemId: 'snapshots', iconCls: 'fa fa-history', apiUrl: url, ownerPanel: me });
            me.items.push({ xtype: 'pveACLView', title: translate('Permissions'), itemId: 'permissions', iconCls: 'fa fa-unlock', path: '/vms/' + info.vmid });
            me.loadDockerTasks = () => { const tasks = me.down('#tasks'); if (tasks && tasks.store) { tasks.store.load(); } };
            ['resources', 'dns', 'options'].forEach(group => {
                const entry = me.items.find(item => item.itemId === group);
                entry.items[0].listeners = { itemdblclick: (_, record) => editSettings(group, record.get('key')),
                    selectionchange: (_, records) => { const button = me.down('#' + (group === 'resources' ? 'resourceEdit' : group + 'Edit'));
                        if (button) { button.setDisabled(!records.length || records[0].get('key') === 'Settings source' || !editable() || locked()); } } };
            });
            const services = me.items.find(item => item.itemId === 'services');
            me.updateServiceActions = records => {
                ['start', 'stop', 'restart'].forEach(verb => { const button = me.down('#service-' + verb);
                    if (button) { button.setDisabled(!records.length || locked() || current && current.capabilities && !current.capabilities.power ||
                        (records.length && (verb === 'start' ? records[0].get('state') === 'running' : records[0].get('state') !== 'running'))); } });
            };
            services.items[0].listeners = { selectionchange: (_, records) => me.updateServiceActions(records) };
            me.callParent();
            const nativeHelp = me.down('proxmoxHelpButton');
            if (nativeHelp) { nativeHelp.setHandler(help); }
            me.on('afterrender', () => { reload(); me.poll = setInterval(reload, 10000); }, me, { single: true });
            me.on('beforedestroy', () => clearInterval(me.poll));
        },
    });

    Ext.define('PVE.betterdocker.CmdMenu', {
        extend: 'Ext.menu.Menu', showSeparator: false,
        initComponent: function () {
            const me = this, record = me.pveSelNode, info = record.data;
            const running = info.status === 'running' || info.status === 'partial';
            const caps = Ext.state.Manager.get('GuiCap');
            const allowed = Proxmox.UserName === 'root@pam' || Boolean(caps && caps.nodes && caps.nodes['Sys.Console']);
            const act = operation => dockerAction(info, operation);
            me.title = 'DCT ' + info.name;
            me.items = [
                { text: translate('Start'), iconCls: 'fa fa-fw fa-play', disabled: !allowed || info.status === 'running', handler: () => act('start') },
                { text: translate('Shutdown'), iconCls: 'fa fa-fw fa-power-off', disabled: !allowed || !running, handler: () => act('stop') },
                { text: translate('Restart'), iconCls: 'fa fa-fw fa-refresh', disabled: !allowed || !running, handler: () => act('restart') },
                { xtype: 'menuseparator' },
                { text: translate('Console'), iconCls: 'fa fa-fw fa-terminal', disabled: !allowed || !running || !me.workspace,
                    handler: () => {
                        me.workspace.selectById(info.id);
                        const panel = me.workspace.query('pveBetterDockerConfig').find(item =>
                            !item.destroyed && item.pveSelNode.data.id === info.id);
                        if (panel) { panel.selectById('console'); }
                    } },
                { text: translate('Edit deployment'), iconCls: 'fa fa-fw fa-pencil', disabled: !allowed,
                    handler: () => request({ url: '/nodes/' + encodeURIComponent(info.node) + '/docker/' + (info.realId || info.id).split('/')[1],
                        method: 'GET', success: response => Ext.create('PVE.betterdocker.DeploymentEdit', {
                            projectId: (info.realId || info.id).split('/')[1], vmid: info.vmid, nodename: info.node, deployment: response.result.data.deployment,
                        }).show() }) },
                { xtype: 'menuseparator' },
                { text: translate('Remove'), iconCls: 'fa fa-fw fa-trash', disabled: !allowed,
                    handler: () => Ext.Msg.confirm(translate('Remove Docker project'),
                        translate('Remove this project? Volumes and bind-mount data are retained.'), answer => {
                            if (answer === 'yes') { act('delete'); }
                        }) },
            ];
            me.callParent();
        },
    });

    const originalNodeMenuInit = PVE.node.CmdMenu.prototype.initComponent;
    Ext.override(PVE.node.CmdMenu, {
        initComponent: function () {
            originalNodeMenuInit.apply(this, arguments);
            const nodename = this.nodename;
            const createCT = this.getComponent('createct');
            this.insert(createCT ? this.items.indexOf(createCT) + 1 : 2, {
                text: translate('Create DCT'), itemId: 'createdct', iconCls: 'fa bd-docker-icon',
                disabled: Proxmox.UserName !== 'root@pam' && !(Ext.state.Manager.get('GuiCap') || {}).nodes?.['Sys.Console'],
                handler: () => Ext.create('PVE.betterdocker.CreateWizard', { nodename }).show(),
            });
        },
    });

    const originalCmdMenu = PVE.Utils.createCmdMenu;
    PVE.Utils.createCmdMenu = function (view, record, item, index, event) {
        if (!record || record.data.type !== 'docker') { return originalCmdMenu.apply(this, arguments); }
        event.stopEvent();
        // Keep the native tree's right-click selection behavior.
        if (!(view instanceof Ext.tree.View)) { view.select(record); }
        const menu = Ext.create('PVE.betterdocker.CmdMenu', {
            pveSelNode: record, workspace: view.up('pveStdWorkspace'),
        });
        // ExtJS hides the menu before calling the clicked item's handler.
        // Destroying it synchronously clears that handler before it can run.
        menu.on('hide', () => Ext.defer(() => menu.destroy(), 1), menu, { single: true });
        menu.showAt(event.getXY());
        return menu;
    };


    if (PVE.form && PVE.form.ViewSelector) {
        const viewInit = PVE.form.ViewSelector.prototype.initComponent;
        Ext.override(PVE.form.ViewSelector, {
            initComponent: function () {
                viewInit.apply(this, arguments);
                const getFilter = this.getViewFilter;
                this.getViewFilter = function () {
                    const view = getFilter.apply(this, arguments);
                    if (view.id === 'tags' && view.filterfn) {
                        const native = view.filterfn;
                        view.filterfn = record => record.data.type === 'docker' || native(record);
                    }
                    if (view.id === 'tags' && view.getFilterFn) {
                        const native = view.getFilterFn;
                        view.getFilterFn = (...args) => {
                            const filter = native(...args);
                            return record => record.data.type === 'docker' || filter(record);
                        };
                    }
                    return view;
                };
            },
        });
    }
    // Native Tag View clones IDs, while its refresh lookup normalizes only VM/CT IDs.
    // Resolve DCT clones in the existing ResourceStore collection without changing IDs.
    const installResourceLookup = () => {
        const collection = PVE.data && PVE.data.ResourceStore && PVE.data.ResourceStore.data;
        if (!collection || collection.betterdockerLookup) { return; }
        const lookup = collection.get;
        collection.get = function (key) {
            const record = lookup.call(this, key);
            const alias = typeof key === 'string' && key.match(/^(docker\/[0-9a-f]{32})-/);
            return record || (alias ? lookup.call(this, alias[1]) : record);
        };
        collection.betterdockerLookup = true;
    };
    const originalInit = PVE.StdWorkspace.prototype.initComponent;
    const originalContent = PVE.StdWorkspace.prototype.setContent;
    const originalSelect = PVE.StdWorkspace.prototype.selectById;
    const originalLogin = PVE.StdWorkspace.prototype.onLogin;
    Ext.override(PVE.StdWorkspace, {
        selectById: function (resourceId) {
            if (typeof resourceId === 'string' && resourceId.startsWith('docker/')) {
                const tree = this.down('pveResourceTree');
                const root = tree && tree.getStore().getRootNode();
                const clone = root && root.findChildBy(node => node.data.realId === resourceId, undefined, true);
                if (clone) { resourceId = clone.data.id; }
            }
            return originalSelect.apply(this, [resourceId]);
        },
        onLogin: function (data) {
            if (originalLogin) { originalLogin.apply(this, arguments); }
            const button = this.down('#createDocker');
            if (button) { button.setDisabled(!data || !(Proxmox.UserName === 'root@pam' || data.cap && data.cap.nodes && data.cap.nodes['Sys.Console'])); }
        },
        setContent: function (comp) {
            if (comp && comp.pveSelNode && comp.pveSelNode.data.type === 'docker') {
                comp.xtype = 'pveBetterDockerConfig';
            }
            return originalContent.call(this, comp);
        },
        initComponent: function () {
            originalInit.apply(this, arguments);
            installResourceLookup();
            try {
                const user = this.down('#userinfo');
                if (!user || !user.ownerCt) { return; }
                const toolbar = user.ownerCt;
                toolbar.insert(toolbar.items.indexOf(user), {
                    xtype: 'button', itemId: 'createDocker', text: translate('Create DCT'), iconCls: 'fa bd-docker-icon',
                    margin: '3 5 0 0', baseCls: 'x-btn', disabled: Proxmox.UserName !== 'root@pam' && !(Ext.state.Manager.get('GuiCap') || {}).nodes?.['Sys.Console'],
                    handler: () => Ext.create('PVE.betterdocker.CreateWizard').show(),
                });
            } catch (error) { console.error('BetterDocker: cannot add toolbar button', error); }
        },
    });
})();
