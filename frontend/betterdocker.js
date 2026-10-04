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
    const dockerTaskLabels = { create: 'Deploy', update: 'Edit deployment', start: 'Start', stop: 'Shutdown',
        restart: 'Restart', delete: 'Remove', settings: 'Edit resources / options', console: 'Console' };
    const dockerTaskDescriptions = { betterdocker: () => 'Docker - Deployment action (legacy)' };
    Object.entries(dockerTaskLabels).forEach(([action, label]) => {
        dockerTaskDescriptions['betterdocker' + action] = (type, id) =>
            (/^\d+$/.test(String(id)) ? 'DCT ' + id : 'Docker') + ' - ' + label;
    });
    Proxmox.Utils.override_task_descriptions(dockerTaskDescriptions);
    const dockerAction = (info, operation, target) => {
        const execute = () => request({
            url: '/nodes/' + encodeURIComponent(info.node) + '/docker/' + info.id.split('/')[1] + '/action',
            method: 'POST', params: { action: operation }, waitMsgTarget: target,
            success: response => { if (operation === 'delete') { task(response); } },
        });
        if (operation === 'stop' || operation === 'restart') {
            const label = operation === 'stop' ? 'Shutdown' : 'Restart';
            const guest = (info.vmid ? 'DCT ' + info.vmid : 'Docker') + ' (' + info.name + ')';
            Ext.Msg.confirm('Confirm', Ext.htmlEncode(label + ' ' + guest + '?'), answer => {
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
                    field('vmid', 'DCT ID', defaults.vmid || me.vmid || '', { xtype: 'numberfield',
                        minValue: 100, maxValue: 999999999, allowDecimals: false, readOnly: Boolean(me.projectId),
                        emptyText: 'Next available guest ID' }),
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
                    const data = [{ key: 'Node', value: values.nodename }, { key: 'DCT ID', value: values.vmid || 'Next available guest ID' }, { key: 'Project', value: values.name },
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
                const container = me.down('#container').getValue();
                if (!container) { Ext.Msg.alert('Console', 'Choose a running container.'); return; }
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
                { xtype: 'combobox', itemId: 'container', fieldLabel: 'Container', labelWidth: 65, width: 350,
                    queryMode: 'local', editable: false, displayField: 'name', valueField: 'id',
                    store: { fields: ['id', 'name'], data: [] } },
                { xtype: 'combobox', itemId: 'shell', width: 110, value: '/bin/sh', editable: false,
                    queryMode: 'local', store: [['/bin/sh', '/bin/sh'], ['/bin/bash', '/bin/bash']] },
                { text: 'Connect', handler: connect }, { text: 'Disconnect', handler: disconnect },
            ];
            me.items = [{ xtype: 'component', itemId: 'terminal' }];
            me.callParent();
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
            me.down('#statusPanel').setTitle(Ext.htmlEncode(data.name) + ' (Uptime: ' +
                Proxmox.Utils.format_duration_long(metrics.uptime) + ')');
            update('statusValue', Ext.htmlEncode(data.status));
            update('nodeValue', Ext.htmlEncode(data.node));
            update('serviceValue', data.containers.filter(item => item.state === 'running').length + ' running / ' +
                data.containers.length + ' containers · ' + data.services.length + ' services');
            update('cpuValue', Proxmox.Utils.render_cpu_usage(metrics.cpu, metrics.cpus), Math.min(1, metrics.cpu));
            update('memoryValue', metrics.maxmem ? Proxmox.Utils.render_size_usage(metrics.mem, metrics.maxmem) : '0 B',
                metrics.maxmem ? metrics.mem / metrics.maxmem : 0);
            ['swap', 'disk'].forEach(key => {
                const used = metrics[key], maximum = metrics['max' + key];
                const available = typeof used === 'number' && typeof maximum === 'number';
                const text = !available ? 'Unavailable' : maximum > 0 ? Proxmox.Utils.render_size_usage(used, maximum) :
                    '0.00% (' + Proxmox.Utils.format_size(used) + ' of 0 B)';
                update(key + 'Value', text, available && maximum > 0 ? Math.min(1, used / maximum) : 0);
                me.down('#' + key + 'Value').setPrintBar(available);
            });
            update('ipValue', Ext.htmlEncode(data.address || 'Waiting for DHCP'));
        },
        initComponent: function () {
            const me = this;
            const store = Ext.create('Proxmox.data.RRDStore', {
                rrdurl: '/api2/json' + me.apiUrl + '/rrddata', model: 'pve-rrd-guest',
            });
            const metric = (id, title, icon, bar) => ({ xtype: 'pmxInfoWidget', itemId: id, title,
                iconCls: icon, printBar: Boolean(bar), padding: '2 25', text: 'Loading...' });
            const charts = [
                ['CPU Usage', ['cpu'], ['CPU usage'], 'percent'],
                ['Memory Usage', ['maxmem', 'mem'], ['Total', 'Used'], 'bytes'],
                ['Network Traffic', ['netin', 'netout'], ['Incoming', 'Outgoing'], 'bytes'],
                ['Disk IO', ['diskread', 'diskwrite'], ['Reads', 'Writes'], 'bytes'],
            ].map(([title, fields, fieldTitles, unit]) => ({ xtype: 'proxmoxRRDChart', title,
                pveSelNode: me.pveSelNode, store, fields, fieldTitles, unit,
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
                                Object.assign(metric('diskValue', 'Bootdisk size', 'fa fa-hdd-o fa-fw', true), {
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
            const identifier = info.id.split('/')[1];
            const url = '/nodes/' + encodeURIComponent(info.node) + '/docker/' + identifier;
            let current;
            let selectedService;
            let loading = false;
            const set = (id, value) => { const item = me.down('#' + id); if (item) { item.setValue(value); } };
            const rows = (id, data) => { const item = me.down('#' + id); if (item) { item.getStore().loadData(data); } };
            const service = () => current && current.services.find(item => item.name === selectedService);
            const populate = () => {
                if (!current) { return; }
                const running = current.containers.some(item => item.state === 'running');
                [['startButton', current.status === 'running'], ['stopButton', !running], ['restartButton', !running]].forEach(([id, disabled]) => {
                    const button = me.down('#' + id); if (button) { button.setDisabled(disabled); }
                });
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
                    rows('resourceGrid', [
                        { key: 'Memory', value: values.memory ? values.memory + ' MiB' : 'Unlimited' },
                        { key: 'Swap', value: values.memory ? values.swap + ' MiB' : 'Docker default' },
                        { key: 'CPU limit', value: values.cpus || 'Unlimited' },
                        { key: 'CPU units', value: values.cpuunits || 'Docker default' },
                        { key: 'PID limit', value: values.pids || 'Unlimited' },
                    ].concat(active.mounts.map((mount, index) => ({ key: 'Mount Point (' + index + ')', value: (mount.source || '') + ',mp=' + mount.target + (mount.read_only ? ',ro=1' : '') }))));
                    rows('dnsGrid', [{ key: 'DNS servers', value: values.dns.join(', ') || 'Docker default' },
                        { key: 'Search domains', value: values.dns_search.join(', ') || 'Docker default' }]);
                    rows('optionGrid', [{ key: 'Restart policy', value: values.restart }, { key: 'Image', value: active.image }]);
                }
                set('source', current.deployment.mode === 'compose' ? current.deployment.compose :
                    'Image: ' + current.deployment.image + '\nPublished ports:\n' + (current.deployment.ports || 'None'));
                const consoleCombo = me.down('#container');
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
            const act = operation => dockerAction(info, operation, me);
            const editDeployment = () => request({ url, method: 'GET', success: response => {
                Ext.create('PVE.betterdocker.CreateWizard', { projectId: identifier, vmid: info.vmid, nodename: info.node,
                    deployment: response.result.data.deployment }).show();
            } });
            const editSettings = (group, selectedKey) => {
                const active = service();
                if (!active) { return; }
                if (selectedKey && selectedKey.startsWith('Mount Point')) { editDeployment(); return; }
                const values = active.settings;
                const numeric = (name, label, maximum, extra) => field(name, label, values[name] || 0,
                    Object.assign({ xtype: 'numberfield', minValue: 0, maxValue: maximum, allowDecimals: false, allowBlank: false }, extra));
                let fields = group === 'resources' ? [
                    numeric('memory', 'Memory (MiB)', 1048576), numeric('swap', 'Swap (MiB)', 1048576, { minValue: -1 }),
                    numeric('cpus', 'CPU limit', 8192, { allowDecimals: true, decimalPrecision: 3 }),
                    numeric('cpuunits', 'CPU units', 262144), numeric('pids', 'PID limit', 4194304),
                    { xtype: 'displayfield', value: 'Limits apply to each container of this service. Zero CPU, memory or PID limit means unlimited. With a memory limit, zero swap disables swap; -1 allows unlimited swap.' },
                ] : group === 'dns' ? [
                    field('dns', 'DNS servers', values.dns.join(', ')), field('dns_search', 'Search domains', values.dns_search.join(', ')),
                    { xtype: 'displayfield', value: 'Separate entries with commas. Empty fields use Docker defaults.' },
                ] : [field('restart', 'Restart policy', values.restart, { xtype: 'combobox', editable: false, queryMode: 'local',
                    store: [['no', 'No'], ['always', 'Always'], ['unless-stopped', 'Unless stopped'], ['on-failure', 'On failure']] })];
                if (group === 'resources' && selectedKey) {
                    const names = selectedKey === 'Memory' || selectedKey === 'Swap' ? ['memory', 'swap'] :
                        selectedKey === 'PID limit' ? ['pids'] : ['cpus', 'cpuunits'];
                    fields = fields.filter(item => !item.name || names.includes(item.name));
                }
                const win = Ext.create('Ext.window.Window', { title: 'Edit ' + group + ' — ' + active.name,
                    modal: true, width: 520, layout: 'fit', items: [{ xtype: 'form', bodyPadding: 12, items: fields }],
                    buttons: [{ text: 'OK', handler: () => {
                        const form = win.down('form').getForm();
                        if (!form.isValid()) { return; }
                        const changed = form.getValues();
                        const settings = Object.assign({}, values);
                        Object.keys(changed).forEach(key => { settings[key] = group === 'resources' ? Number(changed[key]) : changed[key]; });
                        if (group === 'dns') {
                            ['dns', 'dns_search'].forEach(key => { settings[key] = changed[key].split(',').map(v => v.trim()).filter(Boolean); });
                        }
                        request({ url: url + '/settings', method: 'PUT', waitMsgTarget: win,
                            params: { service: active.name, settings: JSON.stringify(settings) },
                            success: response => { task(response); win.close(); } });
                    } }, { text: 'Cancel', handler: () => win.close() }],
                });
                win.show();
            };
            const selector = () => ({ xtype: 'combobox', itemId: 'serviceSelector', fieldLabel: 'Service', labelWidth: 50,
                queryMode: 'local', editable: false, displayField: 'name', valueField: 'name',
                store: { fields: ['name'], data: [] }, listeners: { select: combo => { selectedService = combo.getValue(); populate(); } } });
            const grid = (id, columns, fields) => ({ xtype: 'grid', itemId: id, minHeight: 150,
                store: { fields, data: [] }, columns: columns.map(column => Object.assign({ flex: 1, renderer: Ext.htmlEncode }, column)) });
            const properties = id => Object.assign(grid(id, [{ text: 'Name', dataIndex: 'key', width: 200, flex: 0,
                renderer: (value, meta) => {
                    const icons = { Memory: 'pmx-itype-icon-memory', Swap: 'fa fa-refresh', 'CPU limit': 'pmx-itype-icon-processor',
                        'CPU units': 'pmx-itype-icon-processor', 'PID limit': 'fa fa-cubes' };
                    const icon = icons[value] || (String(value).startsWith('Mount Point') ? 'fa fa-hdd-o' : 'fa fa-cog');
                    meta.tdCls = 'pve-itype-fa';
                    return '<i class="pve-grid-fa fa-fw ' + icon + '"></i> ' + Ext.htmlEncode(value);
                } }, { text: 'Value', dataIndex: 'value' }], ['key', 'value']), { hideHeaders: true });
            const card = (title, id, icon, items, tbar, extra) => Object.assign({ xtype: 'panel', title, itemId: id,
                iconCls: 'fa fa-' + icon, layout: 'anchor', scrollable: true, bodyPadding: 0,
                defaults: { anchor: '100%' }, items, tbar,
                listeners: { afterrender: reload, activate: reload } }, extra);
            me.title = 'Docker ' + (info.vmid ? info.vmid + ' ' : '') + '(' + info.name + ') on node \'' + info.node + '\'';
            me.hstateid = 'betterdocker-' + identifier;
            me.tbar = [
                { text: 'Start', itemId: 'startButton', iconCls: 'fa fa-play', handler: () => act('start') },
                { xtype: 'splitbutton', text: 'Shutdown', itemId: 'stopButton', iconCls: 'fa fa-power-off', handler: () => act('stop'),
                    menu: [{ text: 'Restart', itemId: 'restartButton', iconCls: 'fa fa-refresh', handler: () => act('restart') }] },
                { text: 'Console', iconCls: 'fa fa-terminal', handler: () => me.selectById('console') },
                { text: 'More', menu: [{ text: 'Edit deployment', iconCls: 'fa fa-fw fa-pencil', handler: editDeployment },
                    { text: 'Refresh', iconCls: 'fa fa-fw fa-refresh', handler: reload },
                    { text: 'Remove', iconCls: 'fa fa-trash', handler: () => Ext.Msg.confirm('Remove Docker project',
                        'Remove this project? Volumes and bind-mount data are retained.', answer => { if (answer === 'yes') { act('delete'); } }) }] },
            ];
            me.items = [
                { xtype: 'pveBetterDockerSummary', title: 'Summary', itemId: 'summary', iconCls: 'fa fa-book',
                    apiUrl: url, projectName: info.name, pveSelNode: me.pveSelNode, listeners: { afterrender: reload } },
                { xtype: 'pveBetterDockerConsole', title: 'Console', itemId: 'console', iconCls: 'fa fa-terminal', apiUrl: url,
                    listeners: { afterrender: reload } },
                card('Resources', 'resources', 'cube', [properties('resourceGrid')],
                    [{ text: 'Edit', handler: () => {
                        const grid = me.down('#resourceGrid');
                        const selected = grid.getSelectionModel().getSelection()[0];
                        editSettings('resources', selected && selected.get('key'));
                    } }, '->', selector()], { layout: 'fit' }),
                card('Network', 'network', 'exchange', [Object.assign(grid('networkGrid', [
                    { text: 'Name', dataIndex: 'name', width: 80, flex: 0 }, { text: 'Bridge', dataIndex: 'bridge' },
                    { text: 'MAC address', dataIndex: 'hwaddr' }, { text: 'IPv4', dataIndex: 'address' },
                    { text: 'IPv4 mode', dataIndex: 'mode' }, { text: 'MTU', dataIndex: 'mtu' }],
                    ['name', 'bridge', 'hwaddr', 'address', 'mode', 'mtu']), { flex: 1 }),
                    Object.assign(grid('portGrid', [{ text: 'Service', dataIndex: 'service' }, { text: 'IP address', dataIndex: 'host_ip' },
                        { text: 'Published port', dataIndex: 'published' }, { text: 'Container port', dataIndex: 'target' },
                        { text: 'Protocol', dataIndex: 'protocol' }], ['service', 'host_ip', 'published', 'target', 'protocol']), { title: 'Published ports', flex: 1 })],
                    [{ text: 'Edit ports', handler: editDeployment }], { layout: { type: 'vbox', align: 'stretch' } }),
                card('DNS', 'dns', 'globe', [properties('dnsGrid')], [{ text: 'Edit', handler: () => editSettings('dns') }, '->', selector()], { layout: 'fit' }),
                card('Options', 'options', 'cog', [properties('optionGrid')], [{ text: 'Edit', handler: () => editSettings('options') }, '->', selector()], { layout: 'fit' }),
                card('Deployment', 'deployment', 'file-code-o', [{ xtype: 'textarea', itemId: 'source', readOnly: true, fieldStyle: 'font-family:monospace' }],
                    [{ text: 'Edit', handler: editDeployment }], { layout: 'fit' }),
                card('Logs', 'logs', 'file-text-o', [{ xtype: 'textarea', itemId: 'logsText', readOnly: true, fieldStyle: 'font-family:monospace' }],
                    [{ text: 'Refresh', handler: () => request({ url: url + '/logs', method: 'GET', success: response => set('logsText', response.result.data) }) }],
                    { layout: 'fit', listeners: { afterrender: () => request({ url: url + '/logs', method: 'GET', success: response => set('logsText', response.result.data) }) } }),
                card('Task History', 'tasks', 'list-alt', [grid('taskGrid', [{ text: 'Start time', dataIndex: 'starttime',
                    renderer: value => new Date(value * 1000).toLocaleString() }, { text: 'Description', dataIndex: 'type',
                        renderer: (value, metadata, record) => Ext.htmlEncode(Proxmox.Utils.format_task_description(value, record.get('id'))) },
                    { text: 'User', dataIndex: 'user' }, { text: 'Status', dataIndex: 'status' }], ['upid', 'starttime', 'type', 'id', 'user', 'status'])],
                    [{ text: 'Refresh', handler: () => me.loadDockerTasks() }],
                    { layout: 'fit', listeners: { afterrender: panel => {
                        panel.down('grid').on('itemdblclick', (_, record) => Ext.create('Proxmox.window.TaskViewer', { autoShow: true, upid: record.get('upid') }));
                        me.loadDockerTasks();
                    } } }),
            ];
            const history = me.items.splice(me.items.findIndex(item => item.itemId === 'tasks'), 1)[0];
            me.items.splice(6, 0, history);
            me.loadDockerTasks = () => request({ url: '/nodes/' + encodeURIComponent(info.node) + '/tasks', method: 'GET',
                params: { limit: 200 }, success: response => rows('taskGrid', response.result.data.filter(item =>
                    item.type.startsWith('betterdocker') && (item.id === identifier || String(item.id) === String(info.vmid)))) });
            ['resources', 'dns', 'options'].forEach(group => {
                const entry = me.items.find(item => item.itemId === group);
                entry.items[0].listeners = { itemdblclick: (_, record) => editSettings(group, record.get('key')) };
            });
            me.callParent();
            me.on('afterrender', () => { reload(); me.poll = setInterval(reload, 10000); }, me, { single: true });
            me.on('beforedestroy', () => clearInterval(me.poll));
        },
    });

    Ext.define('PVE.betterdocker.CmdMenu', {
        extend: 'Ext.menu.Menu', showSeparator: false,
        initComponent: function () {
            const me = this, record = me.pveSelNode, info = record.data;
            const running = info.status === 'running' || info.status === 'partial';
            const allowed = Proxmox.UserName === 'root@pam';
            const act = operation => dockerAction(info, operation);
            me.title = 'Docker ' + info.name;
            me.items = [
                { text: 'Start', iconCls: 'fa fa-fw fa-play', disabled: !allowed || info.status === 'running', handler: () => act('start') },
                { text: 'Shutdown', iconCls: 'fa fa-fw fa-power-off', disabled: !allowed || !running, handler: () => act('stop') },
                { text: 'Restart', iconCls: 'fa fa-fw fa-refresh', disabled: !allowed || !running, handler: () => act('restart') },
                { xtype: 'menuseparator' },
                { text: 'Console', iconCls: 'fa fa-fw fa-terminal', disabled: !allowed || !running || !me.workspace,
                    handler: () => {
                        me.workspace.selectById(info.id);
                        const panel = me.workspace.query('pveBetterDockerConfig').find(item =>
                            !item.destroyed && item.pveSelNode.data.id === info.id);
                        if (panel) { panel.selectById('console'); }
                    } },
                { text: 'Edit deployment', iconCls: 'fa fa-fw fa-pencil', disabled: !allowed,
                    handler: () => request({ url: '/nodes/' + encodeURIComponent(info.node) + '/docker/' + info.id.split('/')[1],
                        method: 'GET', success: response => Ext.create('PVE.betterdocker.CreateWizard', {
                            projectId: info.id.split('/')[1], vmid: info.vmid, nodename: info.node, deployment: response.result.data.deployment,
                        }).show() }) },
                { xtype: 'menuseparator' },
                { text: 'Remove', iconCls: 'fa fa-fw fa-trash', disabled: !allowed,
                    handler: () => Ext.Msg.confirm('Remove Docker project',
                        'Remove this project? Volumes and bind-mount data are retained.', answer => {
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
                text: 'Create DCT', itemId: 'createdct', iconCls: 'fa bd-docker-icon',
                disabled: Proxmox.UserName !== 'root@pam',
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
