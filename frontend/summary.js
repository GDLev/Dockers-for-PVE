Ext.define('PVE.betterdocker.Notes', {
    extend: 'Proxmox.panel.NotesView', alias: 'widget.pveBetterDockerNotes',
    maxLength: 8192, canEdit: false,
    run_editor: function () { if (this.canEdit) { this.callParent(); } },
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
        const me = this, metrics = data.metrics || {};
        const update = (id, text, usage) => me.down('#' + id)?.updateValue(text, usage);
        if (me.down('#haValue')) { update('haValue', PVE.Utils.format_ha(data.ha || {})); }
        const storage = data.storage || {};
        const diskAvailable = !data.template && storage.mounted && Number.isFinite(storage.used);
        me.down('#dataDiskValue')?.setPrintBar(Boolean(diskAvailable && storage.limit_mib > 0));
        update('dataDiskValue', diskAvailable ? Proxmox.Utils.render_size_usage(storage.used, storage.limit_mib * 1048576) :
            storage.limit_mib ? Proxmox.Utils.format_size(storage.limit_mib * 1048576) : '',
            diskAvailable && storage.limit_mib > 0 ? Math.min(1, storage.used / (storage.limit_mib * 1048576)) : 0);
        if (data.template) {
            me.down('#statusPanel').setTitle(Ext.htmlEncode(data.name));
            update('nodeValue', Ext.htmlEncode(data.node));
            update('cpuValue', Number.isFinite(metrics.cpus) ? String(metrics.cpus) : '');
            update('swapValue', Number.isFinite(metrics.maxswap) ? Proxmox.Utils.format_size(metrics.maxswap) : '');
            update('memoryValue', Number.isFinite(metrics.maxmem) ? Proxmox.Utils.format_size(metrics.maxmem) : '');
            return;
        }
        if (data.ha_recovery) {
            ['cpuValue', 'memoryValue', 'swapValue', 'diskValue'].forEach(id => me.down('#' + id).setPrintBar(false));
            me.down('#statusPanel').setTitle(Ext.htmlEncode(data.name));
            ['statusValue', 'nodeValue', 'serviceValue', 'cpuValue', 'memoryValue', 'swapValue', 'diskValue', 'ipValue'].forEach(id => update(id, '', 0));
            update('statusValue', Ext.htmlEncode(statusText('stopped')));
            update('nodeValue', Ext.htmlEncode(data.node));
            return;
        }
        me.down('#statusPanel').setTitle(Ext.htmlEncode(data.name) + (Number.isFinite(metrics.uptime) && metrics.uptime > 0 ? ' (' + translate('Uptime') + ': ' +
            Proxmox.Utils.format_duration_long(metrics.uptime) + ')' : ''));
        update('statusValue', Ext.htmlEncode(statusText(data.status)));
        update('nodeValue', Ext.htmlEncode(data.node));
        update('serviceValue', (data.containers || []).filter(item => item.state === 'running').length + ' / ' + (data.containers || []).length);
        const cpuAvailable = Number.isFinite(metrics.cpu) && Number.isFinite(metrics.cpus) && metrics.cpus > 0;
        me.down('#cpuValue').setPrintBar(cpuAvailable);
        update('cpuValue', cpuAvailable ? Proxmox.Utils.render_cpu_usage(metrics.cpu, metrics.cpus) : '',
            cpuAvailable ? Math.min(1, metrics.cpu) : 0);
        const memoryAvailable = Number.isFinite(metrics.mem) && Number.isFinite(metrics.maxmem);
        me.down('#memoryValue').setPrintBar(memoryAvailable && metrics.maxmem > 0);
        update('memoryValue', !memoryAvailable ? '' : metrics.maxmem > 0 ?
            Proxmox.Utils.render_size_usage(metrics.mem, metrics.maxmem) : Proxmox.Utils.format_size(metrics.mem),
            memoryAvailable && metrics.maxmem > 0 ? Math.min(1, metrics.mem / metrics.maxmem) : 0);
        ['swap', 'disk'].forEach(key => {
            const used = metrics[key], maximum = metrics['max' + key];
            const available = Number.isFinite(used) && Number.isFinite(maximum);
            const text = !available ? '' : key === 'disk' ? Proxmox.Utils.format_size(used) : maximum > 0 ? Proxmox.Utils.render_size_usage(used, maximum) :
                Proxmox.Utils.format_size(used);
            me.down('#' + key + 'Value').setPrintBar(key !== 'disk' && available && maximum > 0);
            update(key + 'Value', text, available && maximum > 0 ? Math.min(1, used / maximum) : 0);
        });
        const waitingForDHCP = data.ipv4mode === 'dhcp' && data.status === 'running';
        update('ipValue', Ext.htmlEncode(data.address || (waitingForDHCP ? translate('Waiting for DHCP') : '')));
    },
    initComponent: function () {
        const me = this;
        const template = Boolean(me.pveSelNode.data.template);
        const store = template ? null : Ext.create('Proxmox.data.RRDStore', {
            rrdurl: '/api2/json' + me.apiUrl + '/rrddata', model: 'pve-rrd-guest',
        });
        const metric = (id, title, icon) => ({ xtype: 'pmxInfoWidget', itemId: id, title: translate(title),
            iconCls: icon, printBar: false, padding: '2 25', text: '' });
        const charts = [
            ['CPU Usage', ['cpu'], ['CPU usage'], 'percent'],
            ['Memory Usage', ['maxmem', 'mem'], ['Total', 'Used'], 'bytes'],
            ['Network Traffic', ['netin', 'netout'], ['Incoming', 'Outgoing']],
            ['Disk IO', ['diskread', 'diskwrite'], ['Reads', 'Writes']],
        ].map(([title, fields, fieldTitles, unit]) => Object.assign({ xtype: 'proxmoxRRDChart', title: translate(title),
            pveSelNode: me.pveSelNode, store, fields, fieldTitles: fieldTitles.map(translate),
        }, unit ? { unit } : {}, title === 'Memory Usage' ? {
            powerOfTwo: true, colors: ['#94ae0a', '#115fa6', '#c4c0c0'],
        } : {}));
        me.tbar = template ? [] : ['->', { xtype: 'proxmoxRRDTypeSelector' }];
        me.items = [{ xtype: 'container', itemId: 'itemcontainer', minWidth: 700, layout: 'column',
            defaults: { minHeight: 360, padding: 5 }, items: [
                { xtype: 'container', height: 300, layout: { type: 'hbox', align: 'stretch' }, items: [
                    { xtype: 'panel', itemId: 'statusPanel', title: me.projectName, flex: 1, padding: '0 5 0 0',
                        layout: { type: 'vbox', align: 'stretch' }, items: [
                            { xtype: 'box', height: 20 },
                            metric('statusValue', 'Status', 'fa fa-info fa-fw'),
                            metric('haValue', 'HA State', 'fa fa-heartbeat fa-fw'),
                            metric('nodeValue', 'Node', 'fa fa-building fa-fw'),
                            metric('serviceValue', 'Services', 'fa fa-cubes fa-fw'),
                            { xtype: 'box', height: 10 },
                            metric('cpuValue', 'CPU usage', 'fa fa-fw pmx-itype-icon-processor pmx-icon'),
                            Object.assign(metric('memoryValue', 'Memory usage', 'fa fa-fw pmx-itype-icon-memory pmx-icon'), {
                                warningThreshold: 0.9, criticalThreshold: 0.975,
                            }),
                            metric('dataDiskValue', 'Project data disk', 'fa fa-hdd-o fa-fw'),
                            metric('swapValue', 'SWAP usage', 'fa fa-refresh fa-fw'),
                            Object.assign(metric('diskValue', 'Container root filesystems', 'fa fa-hdd-o fa-fw'), {
                                listeners: { afterrender: widget => { widget.diskTooltip = Ext.create('Ext.tip.ToolTip', { target: widget.getEl(),
                                    html: 'Total size of container root filesystems, including images and writable layers. Volumes and bind mounts are excluded; shared image blocks may be counted for each container.',
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
        if (template) {
            const panels = me.items[0].items[0].items;
            panels[0].padding = panels[1].padding = 5;
            panels[0].items = [
                { xtype: 'box', height: 20 },
                metric('haValue', 'HA State', 'fa fa-heartbeat fa-fw'),
                metric('nodeValue', 'Node', 'fa fa-building fa-fw'),
                { xtype: 'box', height: 20 },
                metric('cpuValue', 'Processors', 'fa fa-fw pmx-itype-icon-processor pmx-icon'),
                metric('memoryValue', 'Memory', 'fa fa-fw pmx-itype-icon-memory pmx-icon'),
                metric('swapValue', 'Swap', 'fa fa-refresh fa-fw'),
                metric('dataDiskValue', 'Project data disk', 'fa fa-hdd-o fa-fw'),
                { xtype: 'box', height: 20 },
            ];
            me.items[0].items = panels;
        }
        me.callParent();
        if (store) { store.startUpdate(); }
        me.on('activate', () => store?.startUpdate());
        me.on('deactivate', () => store?.stopUpdate());
        me.on('show', () => store?.startUpdate());
        me.on('hide', () => store?.stopUpdate());
        // Match native GuestSummary: in-flight RRD callbacks retain the store until completion.
        me.on('destroy', () => store?.stopUpdate());
        me.mon(Ext.state.Manager.getProvider(), 'statechange', (_, key) => {
            if (key === 'summarycolumns') { Proxmox.Utils.updateColumns(me.down('#itemcontainer')); }
        });
    },
});
