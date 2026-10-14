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
        const can = scope => current && current.capabilities ? Boolean(current.capabilities[scope] ?? current.capabilities.configure) : Proxmox.UserName === 'root@pam';
        const editable = () => can('configure');
        const rowEditable = (key, group) => {
            if (/^Mount Point \(mp[0-9]+\)$/.test(key)) { return can('disk') && !current?.ha?.managed && !current?.template && !current?.containers?.some(item => ['running', 'restarting', 'paused'].includes(item.state)); }
            if (['Recovery snapshot', 'Writable layers', 'Preserved files', 'Images (logical size)', 'Logs', 'Snapshots'].includes(key) || /^Mount Point \([0-9]+\)$/.test(key || '')) { return false; }
            if (key === 'Image') { return can('images') && !current?.ha?.managed && !current?.template; }
            if (key === 'Security policy') { return can('hardening') && !current?.ha?.managed && !current?.template && !current?.containers?.some(item => ['running', 'restarting', 'paused'].includes(item.state)); }
            if (key === 'Security') { return can('options') && !current?.ha?.managed && !current?.template; }
            if (key === 'Hookscript') { return can('hookscript'); }
            if (current?.ha?.managed && !['Protection', 'Start at boot', 'Start/Shutdown order'].includes(key)) { return false; }
            if (current?.template && !['Protection', 'Start at boot', 'Start/Shutdown order'].includes(key)) { return false; }
            if (key === 'Container disk limit') { return can('disk') && !current?.protection && Boolean(current?.storage?.rootfs?.supported); }
            if (key === 'Project data disk') { return can('disk') && !current?.protection && !current?.containers?.some(item => ['running', 'restarting', 'paused'].includes(item.state)); }
            const scope = { Memory: 'memory', Swap: 'memory', 'CPU limit': 'cpu', 'CPU units': 'cpu',
                'PID limit': 'options', 'DNS servers': 'network', 'Search domains': 'network',
                'Restart policy': 'options', Protection: 'options', 'Start at boot': 'options', 'Start/Shutdown order': 'options' }[key];
            if (scope) { return can(scope); }
            if (key) { return editable(); }
            return group === 'resources' ? ['cpu', 'memory', 'options', 'disk', 'configure'].some(can) : can(group === 'dns' ? 'network' : 'options');
        };
        const locked = () => Boolean(me.pendingTask || current?.migration || current?.image_update?.phase === 'applying' || current && current.operation && current.operation.active);
        const canOverrule = service => Boolean(current?.capabilities?.overrule_shutdown &&
            (current.operation?.service || null) === (service || null));
        const set = (id, value) => { const item = me.down('#' + id); if (item && item.getValue() !== value) { item.setValue(value); } };
        const activeView = () => me.getLayout?.().getActiveItem() || me;
        const rows = (id, data) => {
            const item = me.down('#' + id); if (!visible(item)) { return; }
            const snapshot = JSON.stringify(data);
            if (item.dockerRowsSnapshot === snapshot) { return; }
            item.dockerRowsSnapshot = snapshot;
            item.setRows(data);
        };
        const service = () => current && current.services.find(item => item.name === selectedService);
        const populate = () => {
            if (!current) { return; }
            const running = current.containers.some(item => ['running', 'restarting', 'paused'].includes(item.state));
            const actions = guestActions(info, current, me.pendingTask);
            const power = { startButton: 'start', stopButton: 'shutdown', restartButton: 'reboot', forceStopButton: 'force-stop', consoleButton: 'console' };
            Object.entries(power).forEach(([id, action]) => {
                const rules = actions[action];
                applyGuestAction(me.down('#' + id), id === 'stopButton' ?
                    Object.assign({}, rules, { disabled: rules.disabled && actions['force-stop'].disabled }) : rules);
            });
            ['console', 'logs'].forEach(id => { const view = me.down('#' + id); if (view) { view.setDisabled(!can('console') || current.template); } });
            me.query('[bdWrite=true]').forEach(button => {
                if (actions[button.bdScope]) { applyGuestAction(button, actions[button.bdScope]); }
                else { button.setDisabled(locked() || !can(button.bdScope || 'configure') || current.template && !button.bdTemplateAllowed || button.bdNeedsStopped && current.containers?.some(item => ['running', 'restarting', 'paused'].includes(item.state))); }
            });
            applyGuestAction(me.down('#removeButton'), actions.remove);
            const recovery = me.down('#recoverButton');
            if (recovery) { recovery.setHidden(!current.recovery?.required); recovery.setDisabled(!can('configure') || !current.recovery?.available); }
            const op = current.operation || {};
            const operationLabel = me.down('#operationLabel');
            if (operationLabel) { operationLabel.setText(current.image_update?.phase === 'applying' ? Ext.htmlEncode(translate('Roll back the recovery snapshot') + ': ' + current.image_update.recovery) : op.active || me.pendingTask ? '<i class="fa fa-lock"></i> ' + Ext.htmlEncode(translate('Operation in progress')) : op.failed ? Ext.htmlEncode(translate(op.message)) : ''); }
            me.snapshotState = current;
            const snapshots = me.down('#snapshots');
            if (visible(snapshots) && snapshots.rendered) { snapshots.updateActions(); }
            const tags = me.down('#dockerTags');
            if (tags) { tags.loadTags(current.tags || ''); tags.canEdit = can('options'); if (tags.getViewModel) { tags.getViewModel().set('canEdit', can('options') && !locked()); } }
            rows('serviceGrid', current.containers.concat(current.services.filter(service => !current.containers.some(container => container.service === service.name)).map(service => ({
                id: 'service:' + service.name, name: '', service: service.name, image: service.image, state: 'stopped', health: service.healthcheck ? 'inactive' : 'none', restarts: 0, exitcode: '',
            }))).map(container => {
                const configured = current.services.find(service => service.name === container.service);
                return Object.assign({}, container, {
                    image: configured?.image_reference || configured?.image || container.image,
                    ports: (configured?.ports || []).map(port => port.published + ':' + port.target + '/' + port.protocol).join(', '),
                });
            }));
            selectedService = service() ? selectedService : (current.services[0] || {}).name;
            me.query('[itemId=serviceSelector]').filter(combo => visible(combo.up('panel'))).forEach(combo => {
                syncStore(combo.getStore(), current.services.map(item => ({ name: item.name })), row => row.name);
                if (combo.getValue() !== selectedService) { combo.setValue(selectedService); }
                combo.setHidden(current.services.length < 2);
            });
            const summary = me.down('#summary');
            if (visible(summary)) { summary.updateSnapshot(current); }
            const networks = current.interfaces || { net0: { bridge: current.bridge, hwaddr: current.hwaddr || '', firewall: current.firewall,
                mtu: current.mtu, address: current.address, cidr: current.cidr, ipv4mode: current.ipv4mode } };
            const pendingNetworks = current.pending_network?.interfaces;
            rows('networkGrid', [...new Set([...Object.keys(networks), ...Object.keys(pendingNetworks || {})])].sort((a, b) => Number(a.slice(3)) - Number(b.slice(3)))
                .map(name => Object.assign({ id: name, name }, networks[name] || {}, {
                    cidr6: networks[name]?.cidr6 || current.endpoint_ipv6?.[name]?.cidr6 || '',
                    address: networks[name]?.cidr || current.endpoint_addresses?.[name] || (name === 'net0' ? current.address : ''),
                    pending: pendingNetworks ? pendingNetworks[name] || { removed: true } : name === 'net0' ? current.pending_network : {},
                })));
            updateNetworkActions();
            rows('portGrid', current.services.flatMap(item => item.ports.map(port => Object.assign({ service: item.name,
                id: JSON.stringify([item.name, port.host_ip || '', port.published, port.target, port.protocol]) }, port))));
            const notes = me.down('#summary')?.down('#notesview');
            if (notes) {
                notes.canEdit = can('options') && !locked();
                if (notes.query) { notes.query('tool').filter(tool => tool.initialConfig?.tooltip === gettext('Edit notes')).forEach(tool => tool.setVisible(notes.canEdit)); }
            }
            const active = service();
            if (active) {
                const values = active.settings;
                const restore = me.down('#restoreCompose'); if (restore) { restore.setDisabled(!active.overridden || !editable() || locked() || Boolean(current.template)); }
                rows('resourceGrid', ['Memory', 'Swap', 'CPU limit', 'CPU units', 'PID limit', 'Container disk limit']
                    .map(key => pendingRow(key, values, active.pending)).concat([
                    { key: 'Project data disk', value: current.storage?.limit_mib ? (current.storage.volid ? current.storage.volid + ', ' : '') + Proxmox.Utils.format_size(current.storage.limit_mib * 1024 * 1024) + (current.storage.mounted ? ' (' + Proxmox.Utils.format_size(current.storage.used) + ' / ' + Proxmox.Utils.format_size(current.storage.available) + ' ' + translate('Available') + ')' : ' — ' + translate('Unavailable')) : 'Unlimited' },
                    { key: 'Writable layers', value: Number.isFinite(current.storage?.usage?.writable) ? Proxmox.Utils.format_size(current.storage.usage.writable) : '' },
                    { key: 'Preserved files', value: Number.isFinite(current.storage?.usage?.preserved) ? Proxmox.Utils.format_size(current.storage.usage.preserved) : '' },
                    { key: 'Images (logical size)', value: Number.isFinite(current.storage?.usage?.images_logical) ? Proxmox.Utils.format_size(current.storage.usage.images_logical) : '' },
                    { key: 'Logs', value: Number.isFinite(current.storage?.usage?.logs) ? Proxmox.Utils.format_size(current.storage.usage.logs) : '' },
                    { key: 'Snapshots', value: Number.isFinite(current.storage?.usage?.snapshots) ? Proxmox.Utils.format_size(current.storage.usage.snapshots) : '' },
                ]).concat((current.storage?.disks || []).map(disk => ({ key: 'Mount Point (' + disk.name + ')', value: disk.volid + ',mp=' + disk.target + ',backup=' + Number(disk.backup !== false) + (disk.read_only ? ',ro=1' : '') }))).concat(active.mounts.map((mount, index) => ({ key: 'Mount Point (' + index + ')', value: (mount.source || '') + ',mp=' + mount.target + (mount.read_only ? ',ro=1' : '') }))));
                rows('dnsGrid', ['DNS servers', 'Search domains'].map(key => pendingRow(key, values, active.pending)));
                rows('optionGrid', [{ key: 'Start at boot', value: Proxmox.Utils.format_boolean(current.onboot || 0) }, { key: 'Start/Shutdown order', value: current.startup || 'order=any' }, { key: 'Protection', value: Proxmox.Utils.format_boolean(current.protection) }, { key: 'Hookscript', value: current.hookscript || Proxmox.Utils.noneText }, pendingRow('Restart policy', values, active.pending), pendingRow('Security', values, active.pending), { key: 'Security policy', value: translate(current.security_policy === 'hardened' ? 'Hardened' : 'Standard') }, { key: 'Image', value: active.image }, ...(current.image_update?.recovery ? [{ key: 'Recovery snapshot', value: current.image_update.recovery }] : [])]);
                ['resourceGrid', 'dnsGrid', 'optionGrid'].forEach(id => {
                    const grid = me.down('#' + id), button = me.down('#' + id + 'Revert');
                    const row = grid?.getSelectionModel().getSelection()[0];
                    if (button) { button.setDisabled(!row || !grid.hasPendingChanges(row.get('key')) || !rowEditable(row.get('key')) || locked()); }
                    const edit = me.down('#' + { resourceGrid: 'resourceEdit', dnsGrid: 'dnsEdit', optionGrid: 'optionsEdit' }[id]);
                    if (edit) { edit.setDisabled(!row || !rowEditable(row.get('key')) || locked()); }
                });
            } else {
                ['resourceGrid', 'dnsGrid', 'optionGrid'].forEach(id => rows(id, []));
                const restore = me.down('#restoreCompose'); if (restore) { restore.setDisabled(true); }
            }
            if (current.deployment && visible(me.down('#source'))) { set('source', current.deployment.mode === 'compose' ? current.deployment.compose :
                'Image: ' + current.deployment.image + '\nPublished ports:\n' + (current.deployment.ports || 'None')); }
            if (me.updateServiceActions) { const grid = me.down('#serviceGrid'); if (grid) { me.updateServiceActions(grid.getSelectionModel().getSelection()); } }
            const consoleCombo = me.down('#consoleContainer');
            if (visible(consoleCombo)) {
                const old = consoleCombo.getValue();
                const containers = current.containers.filter(item => item.state === 'running');
                syncStore(consoleCombo.getStore(), containers, row => row.id);
                const selected = containers.some(item => item.id === old) ? old : (containers[0] || {}).id;
                if (selected !== old) { consoleCombo.setValue(selected); }
            }
        };
        let reloadQueued = false;
        const reload = (force = false) => {
            if (me.destroyed || !visible(me)) { return; }
            if (loading) { reloadQueued = reloadQueued || Boolean(force); return; }
            loading = true;
            const view = activeView();
            const ownsView = ['summary', 'services', 'resources', 'network', 'dns', 'options', 'deployment'].includes(view.itemId);
            const complete = () => {
                loading = false;
                if (reloadQueued && !me.destroyed) { reloadQueued = false; reload(); }
            };
            request({ url: url + '/management', method: 'GET',
                success: response => {
                    if (!me.destroyed) {
                        // Only clear the mask owned by this management request.
                        if (visible(view) && view.dockerManagementError) {
                            Proxmox.Utils.setErrorMask(view, false); view.dockerManagementError = false;
                        }
                        current = response.result.data; populate();
                    }
                    complete();
                },
                failure: response => {
                    if (!me.destroyed && visible(view) && ownsView) {
                        view.dockerManagementError = true; Proxmox.Utils.setErrorMask(view, response.htmlStatus);
                    }
                    complete();
                },
            });
        };
        me.reloadDocker = () => reload(true);
        const networkEditable = () => Boolean(current && can('network') && !locked() && !current.template);
        const updateNetworkActions = () => {
            const selected = me.down('#networkGrid')?.getSelectionModel().getSelection()[0];
            const edit = me.down('#networkEdit'), revert = me.down('#networkRevert');
            me.down('#networkAdd')?.setDisabled(!networkEditable());
            me.down('#networkRemove')?.setDisabled(!networkEditable() || !selected || selected.get('name') === 'net0');
            if (edit) {
                edit.setDisabled(!selected || !networkEditable());
                edit.setTooltip(undefined);
            }
            if (revert) { revert.setDisabled(!selected || !networkEditable() || !Object.keys(current?.pending_network || {}).length); }
        };
        const editNetwork = () => {
            if (!networkEditable() || !me.down('#networkGrid')?.getSelectionModel().getSelection()[0]) { return; }
            openEditor(me, 'network', () => {
                const win = Ext.create('PVE.betterdocker.NetworkEdit', { url: url + '/network', method: 'PUT', nodename: info.node, interfaceName: me.down('#networkGrid').getSelectionModel().getSelection()[0].get('name'), taskDone: reload,
                    apiCallDone: (success, response) => { if (success) { trackTask(response, me); } } });
                win.on('destroy', reload);
                return win;
            });
        };
        const act = operation => { if (!guestActions(info, current, me.pendingTask)[operation].disabled) { dockerAction(info, operation, me); } };
        const editDeployment = () => request({ url, method: 'GET', success: response => {
            Ext.create('PVE.betterdocker.DeploymentEdit', { ownerPanel: me, projectId: identifier, vmid: info.vmid, nodename: info.node,
                deployment: response.result.data.deployment, digest: response.result.data.digest }).show();
        } });
        const editDataDisk = () => {
            if (!can('disk') || locked() || !current) { return; }
            return openEditor(me, 'data-disk', () => {
                const existing = current.storage?.limit_mib || 0, digest = current.digest;
                const win = Ext.create('Proxmox.window.Edit', {
                    title: translate('Edit data disk'), url: url + '/storage-limit', method: 'PUT', fieldDefaults: { labelWidth: 120 },
                    onlineHelp: 'chapter_system_administration', listeners: { afterrender: configureHelp },
                    showTaskViewer: true,
                    items: [{ xtype: 'inputpanel', items: [
                        { xtype: 'pveStorageSelector', name: 'storage', fieldLabel: translate('Storage'), nodename: info.node,
                            storageContent: 'rootdir', value: current.storage?.storage || undefined, allowBlank: Boolean(existing),
                            listeners: { afterrender: selector => {
                                selector.getStore().addFilter(record => ['dir', 'lvmthin', 'zfspool'].includes(record.get('type')) && !record.get('shared'));
                            } } },
                        field('size_mib', 'Data disk (MiB)', existing || 1024,
                            { xtype: 'numberfield', minValue: existing || 256, maxValue: 1048576, allowDecimals: false, allowBlank: false }),
                    ] }],
                    submit: function () {
                        const form = this.formPanel.getForm();
                        if (!form.isValid()) { return; }
                        request({ url: url + '/storage-limit', method: 'PUT', waitMsgTarget: win, params: Object.assign({}, form.getValues(), { digest }),
                            success: response => { trackTask(response, me); task(response); win.close(); } });
                    },
                });
                return win;
            });
        };
        const editMount = diskName => {
            if (!can('disk') || locked() || !current || current.template || current.ha?.managed || current.containers?.some(item => ['running', 'restarting', 'paused'].includes(item.state))) { return; }
            const disk = (current.storage?.disks || []).find(value => value.name === diskName);
            return openEditor(me, 'disk-' + diskName, () => Ext.create('PVE.betterdocker.DiskEdit', {
                autoShow: true, isCreate: !disk, diskName, disk, services: current.services,
                nodename: info.node, digest: current.digest, url: url + '/disks/' + diskName,
                listeners: { destroy: reload },
            }));
        };
        const editSettings = (group, selectedKey) => {
            const mount = /^Mount Point \((mp[0-9]+)\)$/.exec(selectedKey || '');
            if (mount) { return editMount(mount[1]); }
            const active = service();
            if (!active || !rowEditable(selectedKey, group) || locked()) { return; }
            if (selectedKey === 'Settings source') { return; }
            if (selectedKey === 'Project data disk') { editDataDisk(); return; }
            if (selectedKey === 'Security policy') {
                return openEditor(me, 'security-policy', () => Ext.create('PVE.betterdocker.PolicyEdit', {
                    ownerPanel: me, url: url + '/security-policy', digest: current.digest, policy: current.security_policy || 'standard' }));
            }
            if (selectedKey === 'Security') {
                return openEditor(me, 'security:' + active.name, () => Ext.create('PVE.betterdocker.SecurityEdit', {
                    ownerPanel: me, url: url + '/settings', service: active, security: current.security, digest: current.digest }));
            }
            return openEditor(me, 'setting:' + active.name + ':' + ({ Memory: 'memory', Swap: 'memory', 'CPU limit': 'cpu', 'CPU units': 'cpu', 'DNS servers': 'dns', 'Search domains': 'dns' }[selectedKey] || selectedKey), () => {
                if (selectedKey === 'Hookscript') {
                    const digest = current.digest;
                    const win = Ext.create('Proxmox.window.Edit', {
                        subject: gettext('Hookscript'), url: url + '/config', method: 'PUT',
                        items: [{ xtype: 'inputpanel', items: [
                            { xtype: 'pveStorageSelector', name: 'storage', nodename: info.node,
                                storageContent: 'snippets', fieldLabel: gettext('Storage'), submitValue: false,
                                allowBlank: false, value: current.hookscript?.split(':')[0],
                                listeners: { change: function (_, value) {
                                    const selector = this.up('window').down('[name=hookscript]');
                                    if (selector) { selector.setStorage(value); }
                                } } },
                            { xtype: 'pveFileSelector', name: 'hookscript', nodename: info.node, storageContent: 'snippets',
                                fieldLabel: gettext('Hookscript'),
                                listeners: { afterrender: selector => selector.setStorage(current.hookscript?.split(':')[0], info.node) },
                                allowBlank: true, value: current.hookscript || '', emptyText: Proxmox.Utils.noneText },
                        ] }],
                        submit: function () {
                            const values = this.formPanel.getForm().getValues();
                            const params = values.hookscript ? { hookscript: values.hookscript, digest } : { delete: 'hookscript', digest };
                            request({ url: url + '/config', method: 'PUT', waitMsgTarget: win, params,
                                success: () => { win.close(); reload(); } });
                        },
                    });
                    return win;
                }
                if (selectedKey === 'Start/Shutdown order') {
                    return Ext.create('PVE.window.StartupEdit', { url: url + '/config', method: 'PUT',
                        listeners: { destroy: reload } });
                }
                if (selectedKey === 'Protection' || selectedKey === 'Start at boot') {
                    const name = selectedKey === 'Protection' ? 'protection' : 'onboot';
                    const digest = current.digest;
                    const win = Ext.create('Proxmox.window.Edit', {
                        subject: gettext(selectedKey), url: url + '/config', method: 'PUT',
                        items: [{ xtype: 'proxmoxcheckbox', name, uncheckedValue: 0, defaultValue: 0,
                            fieldLabel: gettext('Enabled'), value: Boolean(current[name]) }],
                        submit: function () {
                            request({ url: url + '/config', method: 'PUT', waitMsgTarget: win,
                                params: Object.assign({}, this.formPanel.getForm().getValues(), { digest }),
                                success: () => { win.close(); reload(); } });
                        },
                    });
                    return win;
                }
                if (selectedKey === 'Image' || selectedKey?.startsWith('Mount Point')) { return Ext.create('PVE.betterdocker.DeploymentEdit', { ownerPanel: me, projectId: identifier, vmid: info.vmid, nodename: info.node, deployment: current.deployment, digest: current.digest }); }
                const values = Object.assign({}, active.settings, active.pending), digest = current.digest;
                const numeric = (name, label, maximum, extra) => field(name, label, values[name] || 0,
                    Object.assign({ xtype: 'numberfield', minValue: 0, maxValue: maximum, allowDecimals: false, allowBlank: false }, extra));
                let fields = group === 'resources' ? [
                    numeric('memory', 'Memory (MiB)', 1048576, { step: 32 }), numeric('swap', 'Swap (MiB)', 1048576, { minValue: -1, step: 32 }),
                    numeric('cpus', 'CPU limit', 8192, { allowDecimals: true, decimalPrecision: 3, step: 1 }),
                    numeric('disk', 'Container disk (MiB)', 1048576, { disabled: !current.storage?.rootfs?.supported }),
                    numeric('cpuunits', 'CPU units', 262144), numeric('pids', 'PID limit', 4194304),
                ] : group === 'dns' ? [
                    field('dns_search', 'DNS domain', values.dns_search.join(' '), { emptyText: translate('Docker default'),
                        validator: value => {
                            const domains = value.split(/[ ,;]+/).filter(Boolean);
                            return domains.length <= 8 && domains.every(domain => /^[a-zA-Z0-9_.-]{1,253}$/.test(domain)) || translate('Invalid DNS search domain list');
                        } }),
                    field('dns', 'DNS servers', values.dns.join(' '), { emptyText: translate('Docker default'),
                        validator: value => {
                            const servers = value.split(/[ ,;]+/).filter(Boolean);
                            return servers.length <= 8 && servers.every(server => Proxmox.Utils.IP4_match.test(server) || Proxmox.Utils.IP6_match.test(server)) || translate('Invalid DNS server list');
                        } }),
                ] : [field('restart', 'Restart policy', values.restart, { xtype: 'combobox', editable: false, queryMode: 'local',
                    store: [['no', translate('No')], ['always', translate('Always')], ['unless-stopped', translate('Unless stopped')], ['on-failure', translate('On failure')]] })];
                if (group === 'resources' && selectedKey) {
                    const names = selectedKey === 'Memory' || selectedKey === 'Swap' ? ['memory', 'swap'] :
                        selectedKey === 'PID limit' ? ['pids'] : selectedKey === 'Container disk limit' ? ['disk'] : ['cpus', 'cpuunits'];
                    fields = fields.filter(item => !item.name || names.includes(item.name));
                }
                const fieldScope = { memory: 'memory', swap: 'memory', cpus: 'cpu', cpuunits: 'cpu', pids: 'options', disk: 'disk', dns: 'network', dns_search: 'network', restart: 'options' };
                fields = fields.filter(item => !item.name || can(fieldScope[item.name]));
                const cpu = group === 'resources' && fields.some(item => item.name === 'cpus');
                const input = cpu ? { xtype: 'inputpanel', column1: fields.filter(item => item.name !== 'cpuunits'), column2: [],
                    advancedColumn2: fields.filter(item => item.name === 'cpuunits') } : { xtype: 'inputpanel', items: fields };
                const subject = group === 'resources' ? selectedKey === 'Memory' || selectedKey === 'Swap' ? 'Memory' : cpu ? 'CPU' : selectedKey : { dns: 'DNS', options: 'Options' }[group];
                const win = Ext.create('Proxmox.window.Edit', { title: translate('Edit') + ': ' + translate(subject) + ' - ' + active.name,
                    url: url + '/settings', method: 'PUT', showReset: true, fieldDefaults: { labelWidth: group === 'resources' ? 120 : 100 },
                    onlineHelp: 'chapter_system_administration', listeners: { afterrender: configureHelp },
                    items: [input],
                    submit: function () {
                        const form = this.formPanel.getForm();
                        if (!form.isValid()) { return; }
                        const changed = form.getValues();
                        const settings = {};
                        Object.keys(changed).forEach(key => { settings[key] = group === 'resources' ? Number(changed[key]) : changed[key]; });
                        if (group === 'dns') {
                            ['dns', 'dns_search'].forEach(key => { settings[key] = changed[key].split(/[ ,;]+/).filter(Boolean); });
                        }
                        request({ url: url + '/settings', method: 'PUT', waitMsgTarget: win,
                            params: { service: active.name, settings: JSON.stringify(settings), digest },
                            success: response => { trackTask(response, me); win.close(); } });
                    },
                });
                return win;
            });
        };
        const selector = () => ({ xtype: 'combobox', itemId: 'serviceSelector', fieldLabel: translate('Service'), labelWidth: 50,
            queryMode: 'local', editable: false, displayField: 'name', valueField: 'name',
            store: { fields: ['name'], data: [] }, listeners: { select: combo => { selectedService = combo.getValue(); populate(); } } });
        const grid = (id, columns, fields) => ({ xtype: 'pveBetterDockerTable', itemId: id, minHeight: 150,
            stateId: 'grid-betterdocker-' + id, store: { fields: [...new Set(['id', ...fields])], data: [] }, columns: columns.map(column => Object.assign({ flex: 1, renderer: Ext.htmlEncode }, column)) });
        const properties = id => ({ xtype: 'pveBetterDockerPendingProperties', itemId: id, ownerPanel: me });
        const settingKeys = { Memory: ['memory', 'swap'], Swap: ['swap', 'memory'], 'CPU limit': ['cpus'],
            'CPU units': ['cpuunits'], 'PID limit': ['pids'], 'Container disk limit': ['disk'],
            'DNS servers': ['dns'], 'Search domains': ['dns_search'], 'Restart policy': ['restart'], Security: ['user', 'read_only', 'cap_drop', 'no_new_privileges', 'apparmor'] };
        const pendingRow = (key, values, pending) => {
            const format = values => ({
                Security: [values.user ? 'user=' + values.user : '', values.read_only ? translate('Read-only filesystem') : '',
                    (values.cap_drop || []).length ? 'cap_drop=' + values.cap_drop.join(',') : '', values.no_new_privileges ? translate('No new privileges') : '',
                    values.apparmor ? 'apparmor=' + values.apparmor : ''].filter(Boolean).join('; ') || translate('Docker default'),
                Memory: values.memory ? Proxmox.Utils.format_size(values.memory * 1024 * 1024) : 'Unlimited',
                Swap: values.memory ? values.swap === -1 ? translate('Unlimited') : Proxmox.Utils.format_size(values.swap * 1024 * 1024) : 'Docker default',
                'CPU limit': values.cpus || 'Unlimited', 'CPU units': values.cpuunits || 'Docker default',
                'PID limit': values.pids || 'Unlimited',
                'Container disk limit': values.disk ? Proxmox.Utils.format_size(values.disk * 1024 * 1024) : (current.storage?.rootfs?.supported ? 'Unlimited' : 'Not supported by host'),
                'DNS servers': values.dns.join(', ') || 'Docker default',
                'Search domains': values.dns_search.join(', ') || 'Docker default', 'Restart policy': values.restart,
            })[key];
            const row = { key, value: format(values) };
            if ((settingKeys[key] || []).some(name => Object.hasOwn(pending || {}, name))) {
                row.pending = format(Object.assign({}, values, pending));
            }
            return row;
        };
        const revertButton = gridId => ({ xtype: 'pvePendingRevertButton', itemId: gridId + 'Revert', bdWrite: true,
            handler: function () {
                const grid = me.down('#' + gridId), row = grid.getSelectionModel().getSelection()[0], active = service();
                if (!row || !active || !grid.hasPendingChanges(row.get('key')) || !rowEditable(row.get('key')) || locked()) { return; }
                request({ url: url + '/pending', method: 'PUT', waitMsgTarget: grid,
                    params: { service: active.name, revert: settingKeys[row.get('key')].join(','), digest: current.digest },
                    success: response => { trackTask(response, me); } });
            } });
        const card = (title, id, icon, items, tbar, extra) => Object.assign({ xtype: 'panel', title: translate(title), itemId: id,
            iconCls: 'fa fa-' + icon, layout: 'anchor', scrollable: true, bodyPadding: 0,
            defaults: { anchor: '100%' }, items, tbar,
            listeners: { afterrender: () => reload(), activate: () => reload() } }, extra);
        me.title = 'DCT ' + (info.vmid ? info.vmid + ' ' : '') + '(' + info.name + ') ' + translate('on node') + ' \'' + info.node + '\'';
        me.hstateid = 'betterdocker-' + identifier;
        me.tbarSpacing = false;
        me.tbar = [
            { xtype: 'tbtext', itemId: 'operationLabel', text: translate('') },
            { xtype: 'pveTagEditContainer', itemId: 'dockerTags', cls: 'proxmox-tags-full', tags: info.tags || '', canEdit: Proxmox.UserName === 'root@pam',
                listeners: { change: function (tags) {
                    request({ url: url + '/config', method: 'PUT', params: Object.assign({ tags }, current?.digest ? { digest: current.digest } : {}), success: reload });
                } } }, '->',
            { text: translate('Start'), itemId: 'startButton', iconCls: 'fa fa-play', handler: () => act('start') },
            { xtype: 'splitbutton', text: translate('Shutdown'), itemId: 'stopButton', iconCls: 'fa fa-power-off', handler: () => act('shutdown'),
                menu: [{ text: translate('Reboot'), itemId: 'restartButton', iconCls: 'fa fa-refresh', handler: () => act('reboot') },
                    { text: translate('Stop'), itemId: 'forceStopButton', iconCls: 'fa fa-stop', handler: () => act('force-stop') }] },
            { text: translate('Migrate'), itemId: 'migrateButton', bdScope: 'migrate', hidden: PVE.Utils.isStandaloneNode(), bdWrite: true, bdTemplateAllowed: true, iconCls: 'fa fa-send-o', handler: () => {
                    if (!guestActions(info, current, me.pendingTask).migrate.disabled) { Ext.create('PVE.betterdocker.Migrate', { autoShow: true, nodename: info.node,
                    projectId: identifier, vmid: info.vmid, vmname: info.name, running: Boolean(current?.active_containers || ['running', 'partial'].includes(current?.status)), haManaged: current?.ha?.managed, digest: current?.digest, listeners: { destroy: reload } }); }
            } },
            { xtype: 'splitbutton', text: translate('Console'), itemId: 'consoleButton', iconCls: 'fa fa-terminal', handler: () => { if (!guestActions(info, current).console.disabled) { openConsoleWindow(info); } },
                menu: [{ text: 'xterm.js', iconCls: 'fa fa-terminal', handler: () => { if (!guestActions(info, current).console.disabled) { openConsoleWindow(info); } } }] },
            { text: translate('More'), menu: [
                { text: translate('Clone'), bdScope: 'clone', bdWrite: true, bdTemplateAllowed: true, iconCls: 'fa fa-fw fa-clone', handler: () => {
                    if (!guestActions(info, current, me.pendingTask).clone.disabled) { Ext.create('PVE.betterdocker.StateRestore', { autoShow: true, nodename: info.node,
                        projectId: identifier, vmid: info.vmid, guestname: info.name, isTemplate: current?.template ?? info.template, digest: current?.digest, listeners: { destroy: reload } }); }
                } },
                { text: translate('Convert to template'), bdScope: 'template', bdWrite: true, bdNeedsStopped: true, iconCls: 'fa fa-fw fa-file-o', handler: () => {
                    if (guestActions(info, current, me.pendingTask).template.disabled) { return; }
                    Ext.Msg.confirm(translate('Confirm'), guestConfirmation('template', info), answer => {
                        if (answer === 'yes') { request({ url: url + '/template', method: 'POST', params: { digest: current?.digest },
                            success: response => { trackTask(response, me); } }); }
                    });
                } },
                { text: translate('Manage HA'), bdScope: 'ha', iconCls: 'fa fa-heartbeat', hidden: PVE.Utils.isStandaloneNode(), bdWrite: true,
                    handler: () => { if (!guestActions(info, current, me.pendingTask).ha.disabled) { Ext.create('PVE.ha.VMResourceEdit', { vmid: info.vmid,
                        guestType: 'docker', isCreate: !current?.ha?.managed, listeners: { destroy: reload } }).show(); } } },
                { text: translate('Recover'), itemId: 'recoverButton', hidden: true, disabled: true, iconCls: 'fa fa-wrench', handler: () => {
                    if (can('configure') && current?.recovery?.available) { request({ url: url + '/recover', method: 'POST', success: response => trackTask(response, me) }); }
                } },
                { text: translate('Remove'), itemId: 'removeButton', bdScope: 'remove', disabled: true, bdWrite: true, bdTemplateAllowed: true, iconCls: 'fa fa-trash-o',
                    handler: () => { if (!guestActions(info, current, me.pendingTask).remove.disabled) { removeProject(info, me); } } }] },
        ];
        me.items = [
            { xtype: 'pveBetterDockerSummary', title: translate('Summary'), itemId: 'summary', iconCls: 'fa fa-book',
                apiUrl: url, projectName: info.name, pveSelNode: me.pveSelNode, listeners: { afterrender: () => reload() } },
            { xtype: 'pveBetterDockerConsole', title: translate('Console'), itemId: 'console', iconCls: 'fa fa-terminal', apiUrl: url,
                listeners: { afterrender: () => reload() } },
            card('Services', 'services', 'cubes', [grid('serviceGrid', [
                { text: translate('Service'), dataIndex: 'service' }, { text: translate('Instance'), dataIndex: 'name' },
                { text: translate('Image'), dataIndex: 'image' }, { text: translate('Published ports'), dataIndex: 'ports' }, { text: translate('Status'), dataIndex: 'state', renderer: value => Ext.htmlEncode(statusText(value)) },
                { text: translate('Health'), dataIndex: 'health', renderer: value => Ext.htmlEncode(translate(({ none: 'No healthcheck', inactive: 'Inactive', healthy: 'healthy', unhealthy: 'unhealthy', starting: 'starting' })[value] || value)) },
                { text: translate('AppArmor profile'), dataIndex: 'apparmor', renderer: value => Ext.htmlEncode(value || Proxmox.Utils.noneText) },
                { text: translate('Restarts'), dataIndex: 'restarts', width: 85, flex: 0 },
                { text: translate('Exit code'), dataIndex: 'exitcode', width: 85, flex: 0 },
            ], ['id', 'name', 'service', 'image', 'ports', 'state', 'health', 'apparmor', 'restarts', 'exitcode'])],
                ['start', 'shutdown', 'reboot', 'force-stop'].map(verb => ({ text: translate({ start: 'Start', shutdown: 'Shutdown', reboot: 'Restart', 'force-stop': 'Stop' }[verb]),
                    itemId: 'service-' + verb, disabled: true, handler: () => {
                        const selected = me.down('#serviceGrid').getSelectionModel().getSelection()[0];
                        if (!selected || locked() && !(verb === 'force-stop' && canOverrule(selected.get('service')))) { return; }
                        const execute = (overrule = false) => request({ url: url + '/service-action', method: 'POST',
                            params: Object.assign({ service: selected.get('service'), action: verb }, overrule ? { 'overrule-shutdown': 1 } : {}), success: response => trackTask(response, me) });
                        if (verb === 'force-stop') {
                            confirmStop(Ext.htmlEncode(translate('Stop') + ' ' + selected.get('service') + '?'), canOverrule(selected.get('service')) ? current : null, execute);
                            return;
                        }
                        if (verb === 'start') { execute(); } else { Ext.Msg.confirm(translate('Confirm'),
                            Ext.htmlEncode(translate({ shutdown: 'Shutdown', reboot: 'Restart', 'force-stop': 'Stop' }[verb]) + ' ' + selected.get('service') + '?'), answer => { if (answer === 'yes') { execute(); } }); }
                    } })).concat([{ text: translate('Console'), handler: () => {
                        const record = me.down('#serviceGrid').getSelectionModel().getSelection()[0];
                        me.selectById('console'); const combo = me.down('#consoleContainer');
                        if (record && combo) { combo.setValue(record.get('id')); }
                    } }, { text: translate('Logs'), handler: () => {
                        const record = me.down('#serviceGrid').getSelectionModel().getSelection()[0];
                        me.selectById('logs'); const view = me.down('#logs');
                        if (record && view) { view.setTarget(record.get('service'), record.get('id')); }
                    } }, { text: translate('Update images'), bdWrite: true, bdScope: 'images', handler: () => {
                        if (!can('images') || locked() || current.template || current.ha?.managed) { return; }
                        openEditor(me, 'images', () => Ext.create('PVE.betterdocker.ImageUpdate', { ownerPanel: me, apiUrl: url, nodename: info.node,
                            services: current.services, digest: current.digest, stopped: !current.containers.some(c => ['running', 'restarting', 'paused'].includes(c.state)) }));
                    } }]), { layout: 'fit' }),
            card('Resources', 'resources', 'cube', [properties('resourceGrid')],
                [{ text: translate('Edit'), itemId: 'resourceEdit', disabled: true, handler: () => {
                    const grid = me.down('#resourceGrid');
                    const selected = grid.getSelectionModel().getSelection()[0];
                    if (selected) { editSettings('resources', selected.get('key')); }
                } }, { text: translate('Add'), bdWrite: true, bdScope: 'disk', bdNeedsStopped: true, handler: () => {
                    const used = new Set((current?.storage?.disks || []).map(disk => disk.name));
                    const name = Array.from({ length: 15 }, (_, i) => 'mp' + (i + 1)).find(value => !used.has(value));
                    if (name) { editMount(name); }
                } }, { text: translate('Detach'), bdWrite: true, bdScope: 'disk', bdNeedsStopped: true, handler: () => {
                    const row = me.down('#resourceGrid').getSelectionModel().getSelection()[0];
                    const mount = /^Mount Point \((mp[0-9]+)\)$/.exec(row?.get('key') || '');
                    if (!mount || !rowEditable(row.get('key')) || locked()) { return; }
                    Ext.Msg.confirm(translate('Confirm'), translate('Detach') + ' ' + mount[1] + '?', answer => {
                        if (answer === 'yes') { request({ url: url + '/disks/' + mount[1], method: 'PUT', params: { detach: 1, digest: current.digest },
                            success: response => trackTask(response, me) }); }
                    });
                } }, revertButton('resourceGrid'), { text: translate('Restore Compose'), itemId: 'restoreCompose', bdWrite: true, handler: () => {
                    const active = service(); if (!active || !active.overridden) { return; }
                    request({ url: url + '/reset-settings', method: 'POST', params: { service: active.name, digest: current.digest },
                        success: response => { trackTask(response, me); } });
                } }, '->', selector()], { layout: 'fit' }),
            card('Network', 'network', 'exchange', [Object.assign(grid('networkGrid', [
                { text: translate('Name'), dataIndex: 'name', width: 80, flex: 0 }, { text: translate('Bridge'), dataIndex: 'bridge' },
                { text: translate('MAC address'), dataIndex: 'hwaddr' }, { text: translate('IPv4'), dataIndex: 'address' },
                { text: translate('IPv6'), dataIndex: 'cidr6' }, { text: translate('VLAN Tag'), dataIndex: 'tag' }, { text: translate('Disconnect'), dataIndex: 'link_down' }, { text: translate('Rate limit (MB/s)'), dataIndex: 'rate' }, { text: gettext('Firewall'), dataIndex: 'firewall', renderer: renderNetwork }, { text: translate('MTU'), dataIndex: 'mtu' }].map(column => Object.assign(column, { renderer: renderNetwork })),
                ['name', 'bridge', 'hwaddr', 'address', 'cidr', 'ipv4mode', 'mode', 'mtu', 'firewall', 'pending', 'cidr6', 'tag', 'link_down', 'rate']), { flex: 1, listeners: { itemdblclick: editNetwork, selectionchange: updateNetworkActions, itemkeydown: (_, record, item, index, event) => editKey(editNetwork, record, event) } }),
                Object.assign(grid('portGrid', [{ text: translate('Service'), dataIndex: 'service' }, { text: translate('IP address'), dataIndex: 'host_ip' },
                    { text: translate('Published port'), dataIndex: 'published' }, { text: translate('Container port'), dataIndex: 'target' },
                    { text: translate('Protocol'), dataIndex: 'protocol' }], ['service', 'host_ip', 'published', 'target', 'protocol']), { title: translate('Published ports'), flex: 1 })],
                [{ text: translate('Add'), itemId: 'networkAdd', disabled: true, handler: () => {
                    if (!networkEditable()) { return; }
                    const used = new Set(me.down('#networkGrid').getStore().getRange().map(row => row.get('name')));
                    const name = Array.from({ length: 16 }, (_, i) => 'net' + i).find(value => !used.has(value));
                    if (!name) { return; }
                    Ext.create('PVE.betterdocker.NetworkEdit', { autoShow: true, isCreate: true, interfaceName: name, method: 'PUT', url: url + '/network', nodename: info.node,
                        apiCallDone: (success, response) => { if (success) { trackTask(response, me); } }, listeners: { destroy: reload } });
                 } },
                 { text: translate('Remove'), itemId: 'networkRemove', disabled: true, handler: () => {
                     const selected = me.down('#networkGrid').getSelectionModel().getSelection()[0];
                     if (!networkEditable() || !selected || selected.get('name') === 'net0') { return; }
                     Ext.Msg.confirm(translate('Confirm'), translate('Remove') + ' ' + selected.get('name') + '?', answer => {
                         if (answer === 'yes') { request({ url: url + '/network', method: 'PUT', params: { net: selected.get('name'), delete: 1, digest: current.digest }, success: response => trackTask(response, me) }); }
                     });
                 } },
                 { text: translate('Edit'), itemId: 'networkEdit', disabled: true, handler: editNetwork },
                 { xtype: 'pvePendingRevertButton', itemId: 'networkRevert', disabled: true, handler: () => {
                     if (!networkEditable() || !me.down('#networkGrid').getSelectionModel().getSelection()[0] || !Object.keys(current.pending_network || {}).length) { return; }
                     request({ url: url + '/network', method: 'PUT', params: { revert: 1, digest: current.digest },
                         success: response => trackTask(response, me) });
                 } },
                 { text: translate('Edit ports'), bdWrite: true, handler: editDeployment }], { layout: { type: 'vbox', align: 'stretch' } }),
            card('DNS', 'dns', 'globe', [properties('dnsGrid')], [{ text: translate('Edit'), itemId: 'dnsEdit', disabled: true, handler: () => { const row = me.down('#dnsGrid').getSelectionModel().getSelection()[0]; if (row) { editSettings('dns', row.get('key')); } } }, revertButton('dnsGrid'), '->', selector()], { layout: 'fit' }),
            card('Options', 'options', 'cog', [properties('optionGrid')], [{ text: translate('Edit'), itemId: 'optionsEdit', disabled: true, handler: () => { const row = me.down('#optionGrid').getSelectionModel().getSelection()[0]; if (row) { editSettings('options', row.get('key')); } } }, revertButton('optionGrid'), '->', selector()], { layout: 'fit' }),
            card('Deployment', 'deployment', 'file-code-o', [{ xtype: 'pveBetterDockerCodeArea', itemId: 'source', readOnly: true, fieldStyle: 'font-family:monospace' }],
                [{ text: translate('Edit'), bdWrite: true, handler: editDeployment }, { text: translate('Download'), handler: () => {
                    if (current?.deployment) { download(current.deployment.mode === 'compose' ? 'compose.yaml' : 'deployment.txt', me.down('#source').getValue()); }
                } }], { layout: 'fit' }),
            { xtype: 'pveBetterDockerLogs', title: translate('Logs'), itemId: 'logs', iconCls: 'fa fa-file-text-o', apiUrl: url },
            { xtype: 'proxmoxNodeTasks', title: translate('Task History'), itemId: 'tasks', iconCls: 'fa fa-list-alt',
                nodename: info.node, preFilter: { vmid: info.vmid } },
        ];
        const gui = Ext.state.Manager.get('GuiCap') || {};
        const consoleAccess = Proxmox.UserName === 'root@pam' || Boolean(gui.vms?.['VM.Console']);
        if (!consoleAccess) { me.items = me.items.filter(item => !['logs', 'console'].includes(item.itemId)); }
        const history = me.items.splice(me.items.findIndex(item => item.itemId === 'tasks'), 1)[0];
        me.items.splice(7, 0, history);
        me.items.splice(8, 0, { xtype: 'pveBetterDockerSnapshots', title: translate('Snapshots'), itemId: 'snapshots', iconCls: 'fa fa-history', apiUrl: url, ownerPanel: me });
        me.items.splice(9, 0, { xtype: 'pveBackupView', title: translate('Backup'), itemId: 'backup',
            iconCls: 'fa fa-floppy-o', pveSelNode: me.pveSelNode });
        me.items.splice(10, 0, { xtype: 'pveReplicaView', title: translate('Replication'), itemId: 'replication', iconCls: 'fa fa-retweet', pveSelNode: me.pveSelNode });
        if (Proxmox.UserName === 'root@pam' || gui.vms?.['VM.Audit']) {
            me.items.push(
                { xtype: 'pveFirewallRules', title: gettext('Firewall'), itemId: 'firewall', iconCls: 'fa fa-shield',
                    allow_iface: true, firewall_type: 'vm', base_url: url + '/firewall/rules', list_refs_url: url + '/firewall/refs' },
                { xtype: 'pveBetterDockerFirewallOptions', title: gettext('Options'), itemId: 'firewall-options', groups: ['firewall'],
                    iconCls: 'fa fa-gear', fwtype: 'vm', base_url: url + '/firewall/options' },
                { xtype: 'pveFirewallAliases', title: gettext('Alias'), itemId: 'firewall-aliases', groups: ['firewall'],
                    iconCls: 'fa fa-external-link', base_url: url + '/firewall/aliases' },
                { xtype: 'pveIPSet', title: gettext('IPSet'), itemId: 'firewall-ipset', groups: ['firewall'],
                    iconCls: 'fa fa-list-ol', base_url: url + '/firewall/ipset', list_refs_url: url + '/firewall/refs' });
        }
        if (consoleAccess) { me.items.push({ xtype: 'proxmoxLogView', title: gettext('Log'), itemId: 'firewall-fwlog',
            groups: ['firewall'], iconCls: 'fa fa-list', url: '/api2/extjs' + url + '/firewall/log',
            log_select_timespan: true, submitFormat: 'U' }); }
        me.items.push({ xtype: 'pveACLView', title: translate('Permissions'), itemId: 'permissions', iconCls: 'fa fa-unlock', path: '/vms/' + info.vmid });
        me.loadDockerTasks = () => { const tasks = me.down('#tasks'); if (visible(tasks) && tasks.store) { tasks.store.load(); } };
        ['resources', 'dns', 'options'].forEach(group => {
            const entry = me.items.find(item => item.itemId === group);
            entry.items[0].editRow = record => editSettings(group, record.get('key'));
            entry.items[0].listeners = { itemdblclick: (_, record) => editSettings(group, record.get('key')),
                itemkeydown: (_, record, item, index, event) => editKey(row => editSettings(group, row.get('key')), record, event),
                selectionchange: (_, records) => {
                    const grid = me.down('#' + { resources: 'resourceGrid', dns: 'dnsGrid', options: 'optionGrid' }[group]);
                    const revert = me.down('#' + grid.itemId + 'Revert');
                    if (revert) { revert.setDisabled(!records.length || !grid.hasPendingChanges(records[0].get('key')) || !rowEditable(records[0].get('key')) || locked()); }
                    const button = me.down('#' + (group === 'resources' ? 'resourceEdit' : group + 'Edit'));
                    if (button) { button.setDisabled(!records.length || !rowEditable(records[0].get('key')) || locked()); } } };
        });
        if (info.template) { me.items = me.items.filter(item => !['console', 'logs'].includes(item.itemId)); }
        const initialActions = guestActions(info);
        const actionIds = { startButton: 'start', stopButton: 'shutdown', consoleButton: 'console', migrateButton: 'migrate',
            restartButton: 'reboot', forceStopButton: 'force-stop', removeButton: 'remove' };
        me.tbar.filter(item => item && typeof item === 'object').forEach(button => {
            if (actionIds[button.itemId]) { Object.assign(button, initialActions[actionIds[button.itemId]]); }
            if (Array.isArray(button.menu)) { button.menu.forEach(item => {
                const action = item.bdScope || actionIds[item.itemId];
                if (initialActions[action]) { Object.assign(item, initialActions[action]); }
            }); }
        });
        const services = me.items.find(item => item.itemId === 'services');
        me.updateServiceActions = records => {
            ['start', 'shutdown', 'reboot', 'force-stop'].forEach(verb => { const button = me.down('#service-' + verb);
                if (button) { button.setDisabled(current?.ha?.managed || !records.length || locked() && !(verb === 'force-stop' && canOverrule(records[0]?.get('service'))) || current && current.capabilities && !current.capabilities.power ||
                    (records.length && (verb === 'start' ? current?.template || records[0].get('state') === 'running' : !['running', 'restarting', 'paused'].includes(records[0].get('state'))))); } });
        };
        services.items[0].listeners = { selectionchange: (_, records) => me.updateServiceActions(records) };
        me.callParent();
        const nativeHelp = me.down('proxmoxHelpButton');
        if (nativeHelp) { nativeHelp.setHandler(help); }
        const stopPolling = () => { clearInterval(me.poll); me.poll = null; };
        const startPolling = () => {
            if (me.destroyed) { return; }
            reload();
            // Card owners can still be hidden during afterrender. Keep the
            // timer ready; each tick checks actual visibility after layout.
            if (!me.poll) { me.poll = setInterval(() => { if (!document.hidden && visible(me)) { reload(); } }, 1000); }
        };
        me.on('afterrender', startPolling, me, { single: true });
        me.on('show', startPolling);
        me.on('activate', startPolling);
        me.on('hide', stopPolling);
        me.on('deactivate', stopPolling);
        me.on('beforedestroy', () => { stopPolling(); reloadQueued = false; });
    },
});
