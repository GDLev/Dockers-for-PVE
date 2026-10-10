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
                ["Network", "Network changes made while running take effect at the next full DCT start or reboot. Published ports use this address; routing and outbound traffic use the host network."],
                ["Container disk limit", "Requires overlay2 on XFS with project quotas. Volumes and bind mounts are excluded."],
                ["Project data disk", "Stop services before creating or enlarging the disk. Original data is kept. A mount failure can prevent Docker from starting on the node."],
                ["Snapshots", "Full copies of configuration, container filesystems and owned data. Services are frozen during capture. RAM, logs and external or shared writable mounts are excluded. Rollback leaves DCT stopped."],
                ["Remove", "Persistent data and deployment files are retained."],
            ].map(([label, text]) => '<p><strong>' + Ext.htmlEncode(translate(label)) + '</strong><br>' +
                Ext.htmlEncode(translate(text)) + '</p>').join('') +
                '<p><a href="https://github.com/GDLev/Proxmox-BetterDockers" target="_blank" rel="noopener">' + Ext.htmlEncode(translate('Documentation')) + '</a></p>',
        }] }).show();
    function configureHelp(win) {
        const button = win.down('proxmoxHelpButton');
        if (button) { button.setHandler(help); }
    }
    // All entry points share a window and submission guard. Native Edit owns
    // focus, keyboard submission, Escape, Reset and dirty-form button state.
    const openEditor = (owner, key, create) => {
        owner.dockerEditors = owner.dockerEditors || {};
        const existing = owner.dockerEditors[key];
        if (existing && !existing.destroyed) { existing.toFront?.(); return existing; }
        const win = create();
        if (win.on) {
            owner.dockerEditors[key] = win;
            win.on('destroy', () => { if (owner.dockerEditors[key] === win) { delete owner.dockerEditors[key]; } });
            owner.on?.('beforedestroy', () => win.destroy?.(), owner, { single: true });
        }
        const submit = win.submit, apiCallDone = win.apiCallDone;
        if (submit) {
            win.submit = function () {
                const form = this.formPanel.getForm();
                if (this.dockerSubmitting || !form.isValid() || !this.isCreate && form.isDirty && !form.isDirty()) { return; }
                this.dockerSubmitting = true;
                try { return submit.apply(this, arguments); }
                catch (error) { this.dockerSubmitting = false; throw error; }
            };
            win.apiCallDone = function () { this.dockerSubmitting = false; return apiCallDone?.apply(this, arguments); };
        }
        win.show();
        return win;
    };
    const editKey = (edit, record, event) => {
        if (event.getKey() === event.ENTER) { event.stopEvent(); edit(record); }
    };
    const networkValue = (data, key) => ({
        address: data.ipv4mode === 'dhcp' ? data.address || 'DHCP' : data.cidr || data.address || '',
        mode: data.ipv4mode === 'dhcp' ? 'DHCP' : translate('Static'),
        mtu: data.mtu || translate('Same as bridge'), firewall: Proxmox.Utils.format_boolean(data.firewall),
    })[key] ?? data[key] ?? '';
    const renderNetwork = (value, meta, record) => {
        const key = meta.column?.dataIndex;
        const current = networkValue(record.data, key);
        const pending = record.data.pending;
        const requested = pending && networkValue(Object.assign({}, record.data, pending), key);
        return Ext.htmlEncode(current) + (pending && requested !== current ? '<div style="color:darkorange">' + Ext.htmlEncode(requested) + '</div>' : '');
    };
    // Keep records (and selection) alive when one value changes. ExtJS updates
    // that row; structural changes use its normal add/remove events.
    const syncStore = (store, data, key) => {
        const records = new Map(store.getRange().map(record => [key(record.data), record]));
        const wanted = new Set(data.map(key));
        store.remove(store.getRange().filter(record => !wanted.has(key(record.data))));
        data.forEach(row => {
            const record = records.get(key(row));
            if (!record) { store.add(Object.assign({}, row)); return; }
            const changes = {};
            Object.keys(row).forEach(field => {
                if (JSON.stringify(record.data[field]) !== JSON.stringify(row[field])) { changes[field] = row[field]; }
            });
            if (Object.keys(changes).length) {
                record.beginEdit(); record.set(changes); record.endEdit(true); record.commit();
            }
        });
    };
    const keepGridPosition = (grid, update) => {
        const view = grid.getView(), store = grid.getStore();
        const scrollable = view.getScrollable?.();
        const scroll = scrollable?.getPosition();
        const selection = grid.getSelectionModel?.();
        const selected = selection?.getSelection().slice() || [];
        const navigation = view.getNavigationModel?.();
        const position = navigation?.getPosition();
        const record = position?.record, column = position?.column;
        const focused = view.el?.contains(document.activeElement);
        update();
        const retained = selected.filter(item => store.indexOf(item) >= 0);
        const current = selection?.getSelection() || [];
        if (retained.length && (retained.length !== current.length || retained.some(item => !current.includes(item)))) {
            selection.select(retained);
        }
        // A background refresh must never move focus out of an editor or toolbar.
        if (focused && record && store.indexOf(record) >= 0) { navigation.setPosition(record, column, true); }
        if (scroll) { scrollable.scrollTo(scroll.x, scroll.y); }
    };
    const visible = item => item && !item.destroyed && (!item.isVisible || item.isVisible(true));
    const download = (name, value) => {
        const link = document.createElement('a');
        const url = URL.createObjectURL(new Blob([value], { type: 'text/plain;charset=utf-8' }));
        link.href = url; link.download = name; link.click(); URL.revokeObjectURL(url);
    };
    const importFile = (label, name, maximum) => ({ xclass: 'PVE.betterdocker.FileButton', text: translate(label),
        margin: '0 0 5 0', accept: name === 'compose' ? '.yaml,.yml,text/plain' : '.env,text/plain',
        listeners: { change: async function () {
            const file = this.fileInputEl.dom.files[0];
            if (!file) { return; }
            const revision = this.dockerFileRevision = (this.dockerFileRevision || 0) + 1;
            if (file.size > maximum) { Ext.Msg.alert(translate('Error'), translate('File is too large')); this.reset(true); return; }
            try {
                const content = await file.text();
                if (!this.destroyed && this.dockerFileRevision === revision) { this.up('window').down('[name=' + name + ']').setValue(content); }
            } catch (_) {
                if (!this.destroyed && this.dockerFileRevision === revision) { Ext.Msg.alert(translate('Error'), translate('Could not read file')); }
            } finally {
                if (!this.destroyed && this.dockerFileRevision === revision) { this.reset(true); }
            }
        } } });
    const statusText = value => translate(({ running: 'Running', stopped: 'Stopped', partial: 'Partially running',
        creating: 'Deploying', error: 'Deployment failed', 'network-error': 'Network error', unknown: 'Unknown' })[value] || value);
    const trackTask = (response, target) => {
        const upid = response.result.data;
        if (!upid) { return; }
        target = target || { on: () => {} };
        const node = upid.split(':')[1] && upid.split(':').length > 2 ? upid.split(':')[1] : target.pveSelNode?.data.node;
        if (!node) { return; }
        target.pendingTask = upid;
        let polling = false, finished = false;
        const poll = () => {
            if (target.destroyed || finished) { clearInterval(timer); return; }
            if (polling) { return; }
            polling = true;
            request({ url: '/nodes/' + encodeURIComponent(node) + '/tasks/' + encodeURIComponent(upid) + '/status',
                method: 'GET', failure: () => {
                    // A missed poll is not a failed task. The normal guest refresh
                    // reports connectivity; retry without an extra modal or mask.
                    polling = false;
                }, success: result => {
                    polling = false;
                    if (target.destroyed || finished) { return; }
                    if (result.result.data.status === 'stopped') {
                        finished = true;
                        clearInterval(timer);
                        const currentTask = target.pendingTask === upid;
                        if (currentTask) { target.pendingTask = null; }
                        if (target.reloadDocker) { target.reloadDocker(); }
                        const status = result.result.data.exitstatus;
                        if (currentTask && status !== 'OK' && !response.dockerTaskVisible) {
                            if (/^WARNINGS: \d+$/.test(status)) {
                                Ext.Msg.alert(translate('Warning'), Ext.htmlEncode(status));
                            } else {
                                const showError = message => { if (!target.destroyed) { Ext.Msg.alert(translate('Error'), Ext.htmlEncode(message)); } };
                                request({ url: '/nodes/' + encodeURIComponent(node) + '/tasks/' + encodeURIComponent(upid) + '/log',
                                    method: 'GET', params: { start: 0, limit: 1000 },
                                    success: log => {
                                        const reason = log.result.data.find(row => row.t?.startsWith('BetterDocker: '));
                                        showError(reason ? reason.t.slice('BetterDocker: '.length) : status);
                                    }, failure: () => showError(status) });
                            }
                        }
                    }
                } });
        };
        const timer = setInterval(poll, 1500);
        target.on('beforedestroy', () => clearInterval(timer), target, { single: true });
        if (target.reloadDocker) { target.reloadDocker(); }
    };

    const field = (name, label, value, extra) => Object.assign({
        xtype: 'textfield', name, fieldLabel: translate(label), value, anchor: '100%',
    }, extra || {});
    const imageField = (value, extra) => field('image', 'Image', value, Object.assign({
        allowBlank: false, maxLength: 255, emptyText: 'nginx:stable',
        regex: /^[a-zA-Z0-9][a-zA-Z0-9._/:@-]{0,254}$/,
        regexText: translate('Invalid Docker image'),
    }, extra || {}));
    const request = options => {
        const callback = options.callback;
        return Proxmox.Utils.API2Request(Object.assign({
            failure: response => Ext.Msg.alert(translate('Error'), response.htmlStatus),
        }, options, { callback: function () {
            if (options.waitMsgTarget && typeof options.waitMsgTarget === 'object') { options.waitMsgTarget.dockerSubmitting = false; }
            return callback?.apply(this, arguments);
        } }));
    };
    const task = response => {
        response.dockerTaskVisible = true;
        return Ext.create('Proxmox.window.TaskViewer', { autoShow: true, upid: response.result.data });
    };
    const dockerTaskLabels = { create: 'Create', update: 'Edit deployment', start: 'Start', stop: 'Shutdown',
        restart: 'Restart', shutdown: 'Shutdown', reboot: 'Reboot', forcestop: 'Stop', delete: 'Destroy', settings: 'Edit resources / options', network: 'Network', console: 'Console', snapshotcreate: 'Snapshot', snapshotrestore: 'Rollback', snapshotdelete: 'Delete Snapshot', restore: 'Restore', clone: 'Clone', template: 'Convert to template', migrate: 'Migrate' };
    const dockerTaskDescriptions = { betterdocker: () => 'Docker - Deployment action (legacy)' };
    Object.entries(dockerTaskLabels).forEach(([action, label]) => {
        dockerTaskDescriptions['betterdocker' + action] = (type, id) =>
            (id && !/^[0-9a-f]{32}$/.test(String(id)) ? 'DCT ' + id : 'Docker') + ' - ' + translate(label);
    });
    Proxmox.Utils.override_task_descriptions(dockerTaskDescriptions);
    const guestConfirmation = (action, info) => PVE.Utils.formatGuestTaskConfirmation(
        'betterdocker' + action, info.vmid, info.name);
    // Toolbar and tree menus use the same runtime state and guest privileges.
    const guestActions = (info, state, pendingTask) => {
        const caps = Ext.state.Manager.get('GuiCap') || {};
        const grants = { power: 'VM.PowerMgmt', console: 'VM.Console', clone: 'VM.Clone',
            migrate: 'VM.Migrate', template: 'VM.Allocate', remove: 'VM.Allocate' };
        const allowed = action => state?.capabilities ? Boolean(state.capabilities[action]) :
            Proxmox.UserName === 'root@pam' || Boolean(action === 'ha' ? caps.nodes?.['Sys.Console'] : caps.vms?.[grants[action]]);
        const data = state || info;
        const template = Boolean(data.template);
        const running = Boolean(data.active_containers || data.containers?.some(item => ['running', 'restarting', 'paused'].includes(item.state)) ||
            ['running', 'partial'].includes(data.status));
        const busy = Boolean(pendingTask || data.operation?.active || data.migration);
        const ha = Boolean(state ? data.ha?.managed : data.hastate && data.hastate !== 'unmanaged');
        const override = !ha && !data.operation?.service && Boolean(data.capabilities?.overrule_shutdown);
        const pendingNetwork = Boolean(data.pending_network && Object.keys(data.pending_network).length);
        const fixedAddress = data.ipv4mode !== 'dhcp';
        const pendingSettings = Boolean(data.pending_settings || data.services?.some(item => Object.keys(item.pending || {}).length));
        const restriction = (disabled, tooltip) => ({ disabled, tooltip: disabled ? translate(tooltip) : undefined });
        return {
            start: { hidden: template, disabled: !allowed('power') || template || busy || data.status === 'running' },
            shutdown: { hidden: template, disabled: !allowed('power') || !running || busy },
            reboot: { hidden: template, disabled: !allowed('power') || !running || busy },
            'force-stop': { hidden: template, disabled: !allowed('power') || !running || busy && !override },
            console: { hidden: template, disabled: !allowed('console') || template || !running },
            clone: { hidden: !allowed('clone'), disabled: !allowed('clone') || busy },
            migrate: { hidden: PVE.Utils.isStandaloneNode() || !allowed('migrate'),
                ...restriction(!allowed('migrate') || busy || running && !ha || pendingNetwork || !fixedAddress || Boolean(data.has_snapshots),
                    !fixedAddress ? 'DCT migration requires a static IPv4 address.' : data.has_snapshots ?
                    'DCT migration with snapshot history is not supported.' : pendingNetwork ? 'Apply or revert pending network changes before migrating the DCT.' :
                    running && !ha ? 'Stop the DCT before migration.' : 'Operation in progress') },
            template: { hidden: !allowed('template'),
                ...restriction(!allowed('template') || busy || template || running || ha || Boolean(data.protection) || pendingSettings || pendingNetwork,
                    ha ? 'Remove the DCT from HA before converting it to a template.' :
                    data.protection ? 'Protection mode enabled' : pendingSettings || pendingNetwork ?
                    'Apply or revert pending settings before converting the DCT to a template.' :
                    running ? 'Stop the DCT before converting it to a template.' : 'Operation in progress') },
            remove: restriction(!allowed('remove') || busy || running || ha || Boolean(data.protection) || Boolean(data.replication) ||
                !['stopped', 'error', 'network-error'].includes(data.status),
                ha ? 'Remove the DCT from HA before deleting it.' : data.replication ?
                'Remove replication jobs and their state disk before deleting the DCT.' :
                data.protection ? 'Protection mode enabled' : 'Stop the DCT before removal.'),
            ha: { hidden: PVE.Utils.isStandaloneNode() || !allowed('ha'), disabled: !allowed('ha') || busy },
        };
    };
    const applyGuestAction = (button, action) => {
        if (!button) { return; }
        // Keep the shutdown split menu reachable while an authorized Stop can overrule it.
        button.setDisabled(Boolean(action.disabled));
        if (button.setHidden) { button.setHidden(Boolean(action.hidden)); }
        if (button.setTooltip) { button.setTooltip(action.tooltip); }
    };
    // Use the installed toolkit's renderer, model and DiffStore, as the LXC grids do.
    PVE.Utils.volume_is_dct_backup = data => /(?:^|:)backup\/(?:vzdump-docker-|host\/dct-[1-9][0-9]{2,8}\/)/.test(data?.volid || '');

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

    Ext.define('PVE.betterdocker.Table', {
        extend: 'Ext.grid.Panel', alias: 'widget.pveBetterDockerTable',
        stateful: true, viewConfig: { preserveScrollOnRefresh: true, loadMask: false },
        setRows: function (data) {
            keepGridPosition(this, () => syncStore(this.getStore(), data, row => row.id));
        },
    });

    Ext.define('PVE.betterdocker.PendingProperties', {
        extend: 'Proxmox.grid.PendingObjectGrid', alias: 'widget.pveBetterDockerPendingProperties',
        hideHeaders: true, cwidth1: 200, viewConfig: { preserveScrollOnRefresh: true, loadMask: false },
        initComponent: function () {
            this.rows = {};
            this.rstore = Ext.create('Ext.data.Store', { model: 'KeyValuePendingDelete', proxy: { type: 'memory' } });
            this.callParent();
        },
        setRows: function (data) {
            data.forEach(row => { this.rows[row.key] = { renderer: value => Ext.htmlEncode(translate(value)) }; });
            // The parent filter retains this rows object. Remove obsolete definitions
            // in place, for example when the selected Compose service loses a mount.
            Object.keys(this.rows).filter(key => !data.some(row => row.key === key)).forEach(key => { delete this.rows[key]; });
            const values = data.map(row => Object.assign({ pending: undefined, delete: undefined }, row));
            keepGridPosition(this, () => {
                syncStore(this.rstore, values, row => row.key);
                syncStore(this.getStore(), values, row => row.key);
            });
        },
        reload: function () { this.ownerPanel.reloadDocker(); },
        run_editor: function () { const row = this.getSelectionModel().getSelection()[0]; if (row) { this.editRow(row); } },
        renderKey: function (value, meta) {
            const icons = { Memory: 'pmx-itype-icon-memory', Swap: 'fa fa-refresh', 'CPU limit': 'pmx-itype-icon-processor',
                'CPU units': 'pmx-itype-icon-processor', 'PID limit': 'fa fa-cubes' };
            const icon = icons[value] || (String(value).startsWith('Mount Point') ? 'fa fa-hdd-o' : 'fa fa-cog');
            meta.tdCls = 'pve-itype-fa';
            return '<i class="pve-grid-fa fa-fw ' + icon + '"></i> ' + Ext.htmlEncode(translate(value));
        },
        beforeDestroy: function () { this.rstore.destroy(); this.callParent(); },
    });

    Ext.define('PVE.betterdocker.GuestStop', {
        extend: 'Ext.window.MessageBox', closeAction: 'destroy',
        initComponent: function () {
            this.callParent();
            this.promptContainer.add({ xtype: 'proxmoxcheckbox', name: 'overrule-shutdown',
                checked: this.activeShutdown, boxLabel: translate('Overrule active shutdown tasks'),
                hidden: !this.canOverrule, disabled: !this.canOverrule || this.haManaged, padding: '3 0 0 0' });
        },
        show: function () {
            const me = this;
            me.callParent([{ title: translate('Confirm'), icon: Ext.Msg.WARNING, msg: me.message,
                buttons: Ext.Msg.YESNO, callback: answer => {
                    const field = me.promptContainer.down('[name=overrule-shutdown]');
                    if (answer === 'yes') { me.onConfirm(!field.isDisabled() && Boolean(field.getSubmitValue())); }
                    me.destroy();
                } }]);
        },
    });
    const confirmStop = (message, state, execute) => {
        const caps = Ext.state.Manager.get('GuiCap');
        const activeShutdown = !state?.ha?.managed && Boolean(state?.capabilities?.overrule_shutdown);
        const canOverrule = activeShutdown || Proxmox.UserName === 'root@pam' || Boolean(caps?.nodes?.['Sys.Modify']);
        Ext.create('PVE.betterdocker.GuestStop', { message, activeShutdown, canOverrule, haManaged: Boolean(state?.ha?.managed), onConfirm: execute }).show();
    };
    const dockerAction = (info, operation, target, menuState) => {
        const execute = (overrule = false) => request({
            url: '/nodes/' + encodeURIComponent(info.node) + '/docker/' + (info.realId || info.id).split('/')[1] + '/action',
            method: 'POST', params: Object.assign({ action: operation }, overrule ? { 'overrule-shutdown': 1 } : {}), waitMsgTarget: target,
            success: response => { trackTask(response, target); if (operation === 'delete') { task(response); } },
        });
        if (['shutdown', 'reboot', 'force-stop'].includes(operation)) {
            const message = guestConfirmation(operation === 'force-stop' ? 'forcestop' : operation, info);
            if (operation === 'force-stop') {
                const state = target?.snapshotState || menuState || { ha: { managed: Boolean(info.hastate && info.hastate !== 'unmanaged') } };
                confirmStop(message, state?.operation?.service ? null : state, execute);
                return;
            }
            Ext.Msg.confirm(translate('Confirm'), message, answer => {
                if (answer === 'yes') { execute(); }
            });
            return;
        }
        return execute();
    };
    const removeProject = (info, target) => Ext.create('Proxmox.window.SafeDestroy', {
        autoShow: true, item: { id: info.vmid || info.name,
            formattedIdentifier: PVE.Utils.getFormattedGuestIdentifier(info.vmid, info.name) }, taskName: 'betterdockerdelete',
        url: '/nodes/' + encodeURIComponent(info.node) + '/docker/' + (info.realId || info.id).split('/')[1],
        note: translate('Volumes, bind mounts and deployment files are retained.'), showProgress: false,
        apiCallDone: (success, response) => { if (success) { trackTask(response, target); } },
    });
    const openConsoleWindow = info => Ext.create('Ext.window.Window', {
        autoShow: true, title: translate('Console') + ': DCT ' + (info.vmid || info.name),
        width: 900, height: 500, maximizable: true, constrain: true, layout: 'fit',
        items: [{ xtype: 'pveBetterDockerConsole',
            apiUrl: '/nodes/' + encodeURIComponent(info.node) + '/docker/' + (info.realId || info.id).split('/')[1] }],
    });
    const deployment = values => {
        const common = { name: values.name, mode: values.mode, environment: values.environment || '',
            bridge: values.bridge, address: values.address || '', ipv4mode: values.ipv4mode,
            hwaddr: values.hwaddr || '', mtu: values.mtu || '', firewall: Number(values.firewall) === 1 ? 1 : 0 };
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


    Ext.define('PVE.betterdocker.StorageVolumes', {
        extend: 'Ext.grid.GridPanel', alias: 'widget.pveBetterDockerStorageVolumes',
        onlineHelp: 'chapter_storage',
        initComponent: function () {
            const me = this, node = me.pveSelNode.data.node, storage = me.pveSelNode.data.storage;
            me.store = Ext.create('Ext.data.Store', {
                fields: ['volid', 'vmid', 'dct_name', 'dct_status', 'format', 'size'],
                proxy: { type: 'proxmox', url: '/api2/json/nodes/' + encodeURIComponent(node) + '/storage/' + encodeURIComponent(storage) + '/content',
                    extraParams: { dct: 1 } },
                sorters: [{ property: 'vmid', direction: 'ASC' }, { property: 'volid', direction: 'ASC' }],
            });
            me.columns = [
                { text: gettext('Name'), dataIndex: 'volid', flex: 1, renderer: Ext.htmlEncode },
                { text: translate('DCT ID'), dataIndex: 'vmid', width: 80 },
                { text: translate('Project name'), dataIndex: 'dct_name', width: 170, renderer: Ext.htmlEncode },
                { text: gettext('Status'), dataIndex: 'dct_status', width: 100,
                    renderer: value => translate({ 'in-use': 'In use', unused: 'Unused', retained: 'Retained' }[value] || value) },
                { text: gettext('Format'), dataIndex: 'format', width: 80 },
                { text: gettext('Size'), dataIndex: 'size', width: 100, renderer: Proxmox.Utils.format_size },
            ];
            me.tbar = [{ text: gettext('Reload'), iconCls: 'fa fa-refresh', handler: () => me.store.load() }];
            me.callParent();
            Proxmox.Utils.monStoreErrors(me, me.store);
            me.on('activate', () => me.store.load());
        },
    });
    if (PVE.storage && PVE.storage.ImageView) {
        const imageInit = PVE.storage.ImageView.prototype.initComponent;
        Ext.override(PVE.storage.ImageView, {
            initComponent: function () {
                imageInit.apply(this, arguments);
                this.store.getProxy().setExtraParam('dct', 0);
            },
        });
    }
    if (PVE.storage && PVE.storage.Browser) {
        const configInit = PVE.panel.Config.prototype.initComponent;
        Ext.override(PVE.panel.Config, {
            initComponent: function () {
                if (this.pveSelNode?.data.type === 'storage' && Array.isArray(this.items)) {
                    const index = this.items.findIndex(item => item.itemId === 'contentRootdir');
                    if (index >= 0) {
                        this.items.splice(index + 1, 0, { title: translate('Docker Volumes'), xtype: 'pveBetterDockerStorageVolumes',
                            iconCls: 'fa bd-docker-icon', itemId: 'contentDocker' });
                    }
                }
                configInit.apply(this, arguments);
            },
        });
    }

    Ext.define('PVE.betterdocker.FileButton', {
        extend: 'Ext.form.field.FileButton',
        afterRender: function () {
            this.callParent();
            this.fileInputEl.dom.setAttribute('aria-label', this.text);
        },
        createFileInput: function () {
            this.callParent(arguments);
            this.fileInputEl.dom.setAttribute('aria-label', this.text);
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
    Ext.define('PVE.betterdocker.BridgeSelector', {
        extend: 'PVE.form.BridgeSelector', alias: 'widget.pveBetterDockerBridgeSelector', bridgeType: 'any_bridge',
        initComponent: function () {
            this.callParent();
            this.getStore().addFilter(record => ['bridge', 'vnet'].includes(record.get('type')));
        },
    });

    Ext.define('PVE.betterdocker.FirewallOptions', {
        extend: 'PVE.FirewallOptions', alias: 'widget.pveBetterDockerFirewallOptions',
        add_boolean_row: function (name) {
            this.rows.status = { header: gettext('Status'), required: true, defaultValue: 'unknown',
                renderer: value => gettext({ active: 'Active', inactive: 'Inactive', error: 'Error', unknown: 'Unknown' }[value] || 'Unknown') };
            if (['dhcp', 'ndp', 'radv', 'macfilter', 'ipfilter'].includes(name)) { return; }
            return this.callParent(arguments);
        },
    });

    Ext.define('PVE.betterdocker.NetworkEdit', {
        extend: 'Proxmox.window.Edit', subject: translate('Network Device'), showTaskViewer: false,
        initComponent: function () {
            const me = this;
            me.items = [{ xtype: 'inputpanel',
                column1: [
                    { xtype: 'displayfield', fieldLabel: translate('Name'), value: 'net0' },
                    field('hwaddr', 'MAC address', '', { vtype: 'MacAddress', emptyText: 'auto' }),
                    field('bridge', 'Bridge', '', { xtype: 'pveBetterDockerBridgeSelector', nodename: me.nodename,
                        bridgeType: 'any_bridge', allowBlank: false }),
                ],
                column2: [
                    { xtype: 'radiogroup', fieldLabel: 'IPv4', columns: 2, items: [
                        { xtype: 'radiofield', name: 'ipv4mode', inputValue: 'static', boxLabel: translate('Static'), checked: true },
                        { xtype: 'radiofield', name: 'ipv4mode', inputValue: 'dhcp', boxLabel: 'DHCP' },
                    ], listeners: { change: function (group) {
                        const address = group.up('window').down('[name=cidr]');
                        if (address) { address.setDisabled(me.restoredNetwork || group.getValue().ipv4mode !== 'static'); }
                    } } },
                    field('cidr', 'IPv4/CIDR', '', { vtype: 'IPCIDRAddress', allowBlank: false }),
                    { xtype: 'proxmoxcheckbox', name: 'firewall', fieldLabel: gettext('Firewall'), uncheckedValue: 0 },
                ],
                advancedColumn1: [field('mtu', 'MTU', '', { xtype: 'numberfield', minValue: 576, maxValue: 9216,
                    allowDecimals: false, emptyText: translate('Same as bridge') })],
                onGetValues: function (values) {
                    if (values.ipv4mode === 'dhcp') { values.cidr = ''; }
                    if (me.restoredNetwork) { return { firewall: values.firewall }; }
                    values.mtu = values.mtu || 0;
                    return values;
                },
            }];
            if (me.restoredNetwork) {
                const panel = me.items[0];
                [...panel.column1, ...panel.column2, ...panel.advancedColumn1].filter(item => item.name !== 'firewall').forEach(item => { item.disabled = true; });
            }
            me.callParent();
            // Native Edit.load touches its form before invoking success. A
            // delayed response must not touch a network editor closed with Escape.
            let closed = false;
            me.on('beforedestroy', () => { closed = true; });
            me.setLoading(gettext('Please wait...'));
            Proxmox.Utils.API2Request({ url: me.url, method: 'GET',
                success: function (response) {
                    if (closed) { return; }
                    me.setLoading(false);
                    me.formPanel.getForm().clearInvalid();
                    const values = response.result.data;
                    const requested = Object.assign({}, values, values.pending);
                    if (!requested.mtu) { requested.mtu = ''; }
                    me.setValues(requested);
                    me.digest = values.digest;
                    me.query('radiofield').forEach(item => item.resetOriginalValue());
                },
                failure: function (response) {
                    if (closed) { return; }
                    me.setLoading(false);
                    Ext.Msg.alert(gettext('Error'), response.htmlStatus, () => { if (!closed) { me.close(); } });
                },
            });
        },
    });

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
                    { xtype: 'radiogroup', fieldLabel: translate('IPv4'), columns: 2,
                        items: [
                            { xtype: 'radiofield', name: 'ipv4mode', inputValue: 'static', boxLabel: translate('Static'),
                                checked: (defaults.ipv4mode || (defaults.address ? 'static' : 'dhcp')) === 'static', readOnly: Boolean(me.projectId) },
                            { xtype: 'radiofield', name: 'ipv4mode', inputValue: 'dhcp', boxLabel: translate('DHCP'),
                                checked: (defaults.ipv4mode || (defaults.address ? 'static' : 'dhcp')) === 'dhcp', readOnly: Boolean(me.projectId) },
                        ], listeners: { change: updateNetwork, afterrender: updateNetwork } },
                    field('address', 'IPv4/CIDR', defaults.cidr || defaults.address || '', { allowBlank: false, vtype: 'IPCIDRAddress',
                        disabled: (defaults.ipv4mode || (defaults.address ? 'static' : 'dhcp')) === 'dhcp',
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
                        params: Object.assign({ deployment: JSON.stringify(deployment(values)) }, !wizard.projectId ? { start: Number(values.start) === 1 ? 1 : 0, ...(values.pool ? { pool: values.pool } : {}) } : {}), waitMsgTarget: wizard,
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
                { text: translate('Connect'), itemId: 'consoleConnect', disabled: true, handler: connect }, { text: translate('Disconnect'), handler: disconnect },
            ];
            me.items = [{ xtype: 'component', itemId: 'terminal' }];
            me.callParent();
            const targets = () => request({ url: me.apiUrl + '/management', method: 'GET', success: response => {
                if (me.destroyed) { return; }
                const combo = me.down('#consoleContainer'), selected = combo.getValue();
                const allowed = Boolean(response.result.data.capabilities?.console);
                const containers = allowed ? response.result.data.containers.filter(container => container.state === 'running') : [];
                combo.setDisabled(!allowed);
                me.down('#shell').setDisabled(!allowed);
                me.down('#consoleConnect').setDisabled(!allowed || !containers.length);
                combo.getStore().loadData(containers);
                combo.setValue(containers.some(container => container.id === selected) ? selected : (containers[0] || {}).id);
            } });
            me.on('afterrender', targets, me, { single: true });
            me.on('activate', targets);
            me.on('resize', resize);
            me.on('beforedestroy', disconnect);
        },
    });

    Ext.define('PVE.betterdocker.VMSelectorIntegration', {
        override: 'PVE.form.VMSelector',
        initComponent: function () {
            const me = this;
            me.columnsDeclaration = me.columnsDeclaration.map(column => column.dataIndex === 'type' ?
                Object.assign({}, column, { renderer: function (value, ...args) {
                    return value === 'docker' ? 'DCT' : column.renderer.call(this, value, ...args);
                } }) : column);
            me.callParent();
            me.on('afterrender', function () {
                const win = me.up('window');
                const bulk = ['startall', 'stopall', 'migrateall', 'start', 'shutdown', 'migrate', 'suspend', 'suspendall'].includes(win?.action);
                const backup = win?.isXType && win.isXType('pveDcBackupEdit');
                const filterGuests = (store, records) => {
                    if (!bulk && !backup) { store.remove(records.filter(record => record.get('type') === 'docker')); }
                };
                const store = me.getStore();
                me.mon(store, 'load', filterGuests);
                filterGuests(store, store.getRange());
                if (bulk) {
                    const type = win.down('#typefilter');
                    if (type) { type.getStore().add({ [type.valueField]: 'docker', [type.displayField]: 'DCT' }); }
                    const status = win.down('#statusfilter');
                    if (status) { status.getStore().add({ [status.valueField]: 'partial', [status.displayField]: translate('Partially running') }); }
                }
            }, me, { single: true });
        },
    });

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
            const update = (id, text, usage) => me.down('#' + id).updateValue(text, usage);
            if (me.down('#haValue')) { update('haValue', PVE.Utils.format_ha(data.ha || {})); }
            if (data.ha_recovery) {
                ['cpuValue', 'memoryValue', 'swapValue', 'diskValue'].forEach(id => me.down('#' + id).setPrintBar(false));
                me.down('#statusPanel').setTitle(Ext.htmlEncode(data.name));
                ['statusValue', 'nodeValue', 'serviceValue', 'cpuValue', 'memoryValue', 'swapValue', 'diskValue', 'ipValue'].forEach(id => update(id, '', 0));
                update('statusValue', Ext.htmlEncode(statusText('stopped')));
                update('nodeValue', Ext.htmlEncode(data.node));
                return;
            }
            me.down('#statusPanel').setTitle(Ext.htmlEncode(data.name) + (Number.isFinite(metrics.uptime) ? ' (' + translate('Uptime') + ': ' +
                Proxmox.Utils.format_duration_long(metrics.uptime) + ')' : ''));
            update('statusValue', Ext.htmlEncode(statusText(data.status)));
            update('nodeValue', Ext.htmlEncode(data.node));
            update('serviceValue', data.containers.filter(item => item.state === 'running').length + ' / ' + data.containers.length);
            const cpuAvailable = Number.isFinite(metrics.cpu) && Number.isFinite(metrics.cpus) && metrics.cpus > 0;
            me.down('#cpuValue').setPrintBar(cpuAvailable);
            update('cpuValue', cpuAvailable ? Proxmox.Utils.render_cpu_usage(metrics.cpu, metrics.cpus) : translate('Unavailable'),
                cpuAvailable ? Math.min(1, metrics.cpu) : 0);
            const memoryAvailable = Number.isFinite(metrics.mem) && Number.isFinite(metrics.maxmem);
            me.down('#memoryValue').setPrintBar(memoryAvailable && metrics.maxmem > 0);
            update('memoryValue', !memoryAvailable ? translate('Unavailable') : metrics.maxmem > 0 ?
                Proxmox.Utils.render_size_usage(metrics.mem, metrics.maxmem) : Proxmox.Utils.format_size(metrics.mem),
                memoryAvailable && metrics.maxmem > 0 ? Math.min(1, metrics.mem / metrics.maxmem) : 0);
            ['swap', 'disk'].forEach(key => {
                const used = metrics[key], maximum = metrics['max' + key];
                const available = Number.isFinite(used) && Number.isFinite(maximum);
                const text = !available ? translate('Unavailable') : maximum > 0 ? Proxmox.Utils.render_size_usage(used, maximum) :
                    Proxmox.Utils.format_size(used);
                me.down('#' + key + 'Value').setPrintBar(available && maximum > 0);
                update(key + 'Value', text, available && maximum > 0 ? Math.min(1, used / maximum) : 0);
            });
            update('ipValue', Ext.htmlEncode(data.address || translate('Waiting for DHCP')));
        },
        initComponent: function () {
            const me = this;
            const chartRanges = ['hour', 'day', 'week'];
            const store = Ext.create('Proxmox.data.RRDStore', {
                rrdurl: '/api2/json' + me.apiUrl + '/rrddata', model: 'pve-rrd-guest',
                setRRDUrl: function (timeframe, cf) {
                    const requested = timeframe || this.timeframe;
                    this.timeframe = chartRanges.includes(requested) ? requested : 'hour';
                    Proxmox.data.RRDStore.prototype.setRRDUrl.call(this, this.timeframe, cf);
                },
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
            me.tbar = ['->', { xtype: 'proxmoxRRDTypeSelector', listeners: { afterrender: selector => {
                const timeframe = selector.lookup('timeframe');
                timeframe.getStore().filterBy(record => chartRanges.includes(record.get('id')));
                if (!chartRanges.includes(timeframe.getValue())) { timeframe.setValue('hour'); }
            } } }];
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
                                metric('swapValue', 'SWAP usage', 'fa fa-refresh fa-fw'),
                                Object.assign(metric('diskValue', 'Container root filesystems', 'fa fa-hdd-o fa-fw'), {
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
            me.on('activate', () => store.startUpdate());
            me.on('deactivate', () => store.stopUpdate());
            me.on('show', () => store.startUpdate());
            me.on('hide', () => store.stopUpdate());
            // Match native GuestSummary: in-flight RRD callbacks retain the store until completion.
            me.on('destroy', () => store.stopUpdate());
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
            let content = '', loading = false, queued = false, targetsLoading = false, containers = [];
            const updateContainers = () => {
                const service = me.down('#logService').getValue();
                const combo = me.down('#logContainer'), selected = combo.getValue();
                const matching = containers.filter(container => !service || container.service === service);
                syncStore(combo.getStore(), [{ id: '', name: translate('All instances') }, ...matching], row => row.id);
                const value = matching.some(container => container.id === selected) ? selected : '';
                if (selected !== value) { combo.setValue(value); }
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
            const queryParams = () => {
                const params = { tail: me.down('#logTail').getValue() || 200 };
                const service = me.down('#logService').getValue(), container = me.down('#logContainer').getValue();
                if (service) { params.service = service; }
                if (container) { params.container = container; }
                return params;
            };
            me.reloadLogs = () => {
                if (!visible(me)) { return; }
                if (loading) { queued = true; return; }
                loading = true;
                const params = queryParams(), signature = JSON.stringify(params);
                const complete = () => {
                    loading = false;
                    if (queued && visible(me)) { queued = false; me.reloadLogs(); }
                };
                request({ url: me.apiUrl + '/logs', method: 'GET', params,
                    failure: response => {
                        if (visible(me) && signature === JSON.stringify(queryParams())) { Proxmox.Utils.setErrorMask(me, response.htmlStatus); }
                        complete();
                    },
                    success: response => {
                        if (visible(me) && signature === JSON.stringify(queryParams())) {
                            Proxmox.Utils.setErrorMask(me, false); content = response.result.data; render();
                        }
                        complete();
                    } });
            };
            const targets = () => {
                if (!visible(me) || targetsLoading) { return; }
                targetsLoading = true;
                request({ url: me.apiUrl + '/management', method: 'GET',
                    failure: response => { targetsLoading = false; if (visible(me)) { Proxmox.Utils.setErrorMask(me, response.htmlStatus); } },
                    success: response => {
                        targetsLoading = false;
                        if (!visible(me)) { return; }
                        const combo = me.down('#logService'), services = response.result.data.services;
                        syncStore(combo.getStore(), [{ name: '', label: translate('All services') },
                            ...services.map(service => ({ name: service.name, label: service.name }))], row => row.name);
                        if (combo.getValue() && !services.some(service => service.name === combo.getValue())) { combo.setValue(''); }
                        containers = response.result.data.containers; updateContainers();
                    } });
            };
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
            const stop = () => { clearInterval(me.logPoll); me.logPoll = null; queued = false; };
            const start = () => {
                if (!visible(me)) { return; }
                targets(); me.reloadLogs();
                if (!me.logPoll) { me.logPoll = setInterval(() => {
                    if (!document.hidden && visible(me) && me.down('#logAuto').getValue()) { me.reloadLogs(); }
                }, 3000); }
            };
            me.on('afterrender', start, me, { single: true });
            me.on('activate', start);
            me.on('deactivate', stop);
            me.on('hide', stop);
            me.on('beforedestroy', stop);
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

    Ext.define('PVE.betterdocker.SnapshotEdit', {
        extend: 'PVE.window.Snapshot',
        load: function (options) {
            const success = options?.success;
            return this.callParent([Object.assign({}, options, { success: function (response, opts) {
                // Native load has already saved the digest. Keep it out of the
                // snapshot's configuration inventory.
                delete response.result.data.digest;
                return success?.(response, opts);
            } })]);
        },
        submit: function () {
            const me = this, form = me.formPanel.getForm();
            if (me.viewonly || !form.isValid()) { return; }
            const values = form.getValues();
            request({ url: me.url, method: me.isCreate ? 'POST' : 'PUT', waitMsgTarget: me,
                params: me.isCreate ? { action: 'create', snapname: values.snapname, description: values.description || '' } :
                    { description: values.description || '', digest: me.digest },
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
                if (!edit && !vm.get('snapshotAllowed')) { return; }
                const win = openEditor(view.ownerPanel, 'snapshot:' + (edit ? snapname : 'new'), () => Ext.create('PVE.betterdocker.SnapshotEdit', {
                    ownerPanel: view.ownerPanel, nodename: info.node, projectId: (info.realId || info.id).split('/')[1], guestId: info.vmid, vmname: info.name,
                    isCreate: !edit, viewonly: !vm.get('snapshotAllowed'), snapname: edit ? snapname : undefined,
                    submitText: !edit ? translate('Take Snapshot') : undefined,
                }));
                me.mon(win, 'destroy', () => { me.reload(); view.ownerPanel.reloadDocker(); }, me);
            },
            rollback: function () { this.snapshotAction('restore'); },
            remove: function () { this.snapshotAction('delete'); },
            snapshotAction: function (action) {
                const me = this, view = me.getView(), snapname = me.getViewModel().get('selected');
                if (!snapname || snapname === 'current') { return; }
                request({ url: view.apiUrl + '/snapshot', method: 'POST', params: { action, snapname }, waitMsgTarget: view,
                    callback: () => me.reload(), success: response => {
                        response.dockerTaskVisible = true;
                        trackTask(response, view.ownerPanel);
                        Ext.create('Proxmox.window.TaskProgress', { upid: response.result.data, autoShow: true });
                    } });
            },
            reload: function () {
                const me = this, view = me.getView();
                if (me.destroyed || !visible(view) || me.loadingSnapshots) { return; }
                me.loadingSnapshots = true;
                request({ url: view.apiUrl + '/snapshots', method: 'GET', success: response => {
                    me.loadingSnapshots = false;
                    if (me.destroyed || !visible(view)) { return; }
                    Proxmox.Utils.setErrorMask(view, false);
                    const vm = me.getViewModel(), rows = response.result.data, current = rows.find(row => row.name === 'current');
                    vm.set('running', Boolean(current?.running));
                    const digest = current?.digest + ':' + Boolean(current?.running);
                    if (digest !== me.old_digest) {
                        me.old_digest = digest;
                        keepGridPosition(view, () => {
                            const root = view.getRootNode(), nodes = new Map();
                            let structureChanged = false;
                            root.cascadeBy(node => { if (node !== root) { nodes.set(node.get('name'), node); } });
                            const wanted = new Set(rows.map(row => row.name));
                            rows.forEach(row => {
                                const values = Object.assign({}, row, { iconCls: row.name === 'current' ?
                                    'fa fa-cube fa-fw x-fa-tree' : 'fa fa-fw fa-history x-fa-tree' });
                                let node = nodes.get(row.name);
                                if (node) { node.beginEdit(); node.set(values); node.endEdit(true); node.commit(); }
                                else { node = root.appendChild(Object.assign({ leaf: true }, values)); nodes.set(row.name, node); structureChanged = true; }
                            });
                            rows.forEach(row => {
                                const node = nodes.get(row.name), parent = nodes.get(row.parent) || root;
                                if (node.parentNode !== parent) { parent.appendChild(node); structureChanged = true; }
                            });
                            nodes.forEach((node, name) => { if (!wanted.has(name)) { node.remove(); structureChanged = true; } });
                            nodes.forEach((node, name) => {
                                if (!wanted.has(name)) { return; }
                                node.set({ leaf: !node.childNodes.length, expandable: false });
                                if (node.childNodes.length) { node.expand(); }
                            });
                            if (structureChanged) { view.getStore().sort(); }
                        });
                    }
                    view.updateActions(); me.load_task.delay(3000);
                }, failure: response => {
                    me.loadingSnapshots = false;
                    if (!me.destroyed && visible(view)) { Proxmox.Utils.setErrorMask(view, response.htmlStatus); me.load_task.delay(3000); }
                } });
            },
        },
        updateActions: function () {
            const me = this, vm = me.getViewModel(), owner = me.ownerPanel, data = owner.snapshotState;
            const unlocked = Boolean(data && !data.operation?.active && !data.migration && !owner.pendingTask);
            const supported = Boolean(data?.containers?.length && data.containers.length === data.expected && data.containers.every(item => ['created', 'exited', 'running'].includes(item.state)));
            vm.set('snapshotAllowed', unlocked && Boolean(data?.capabilities?.snapshot));
            vm.set('rollbackAllowed', unlocked && !data.template && !data.ha?.managed && Boolean(data?.capabilities?.rollback));
            vm.set('snapshotFeature', supported);
        },
        viewConfig: { preserveScrollOnRefresh: true, loadMask: false },
        loadSnapshots: function () { this.getController().reload(); },
        initComponent: function () {
            const me = this;
            me.callParent();
            me.on('activate', () => me.loadSnapshots());
            me.on('deactivate', () => me.getController().cancel());
            me.on('hide', () => me.getController().cancel());
            // The native rollback confirmation uses a VM identifier; retain its wording for DCT.
            const button = me.query('proxmoxButton').find(item => item.text === translate('Rollback'));
            if (button) { button.confirmMsg = function () {
                const info = me.ownerPanel.pveSelNode.data, record = me.getSelectionModel().getSelection()[0];
                return guestConfirmation('snapshotrestore', info) + Ext.htmlEncode(" '" + record.get('name') + "'? " + translate('Current state will be lost.'));
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
            const can = scope => current && current.capabilities ? Boolean(current.capabilities[scope] ?? current.capabilities.configure) : Proxmox.UserName === 'root@pam';
            const editable = () => can('configure');
            const rowEditable = (key, group) => {
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
            const locked = () => Boolean(me.pendingTask || current?.migration || current && current.operation && current.operation.active);
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
                    else { button.setDisabled(locked() || !can(button.bdScope || 'configure') || current.template && !button.bdTemplateAllowed); }
                });
                applyGuestAction(me.down('#removeButton'), actions.remove);
                const op = current.operation || {};
                const operationLabel = me.down('#operationLabel');
                if (operationLabel) { operationLabel.setText(op.active || me.pendingTask ? '<i class="fa fa-lock"></i> ' + Ext.htmlEncode(translate('Operation in progress')) : op.failed ? Ext.htmlEncode(translate(op.message)) : ''); }
                me.snapshotState = current;
                const snapshots = me.down('#snapshots');
                if (visible(snapshots) && snapshots.rendered) { snapshots.updateActions(); }
                const tags = me.down('#dockerTags');
                if (tags) { tags.loadTags(current.tags || ''); tags.canEdit = can('options'); if (tags.getViewModel) { tags.getViewModel().set('canEdit', can('options') && !locked()); } }
                rows('serviceGrid', current.containers.concat(current.services.filter(service => !current.containers.some(container => container.service === service.name)).map(service => ({
                    id: 'service:' + service.name, name: '', service: service.name, image: service.image, state: 'stopped', health: 'none', restarts: 0, exitcode: '',
                }))).map(container => Object.assign({}, container, { ports: (current.services.find(service => service.name === container.service)?.ports || [])
                    .map(port => port.published + ':' + port.target + '/' + port.protocol).join(', ') })));
                selectedService = service() ? selectedService : (current.services[0] || {}).name;
                me.query('[itemId=serviceSelector]').filter(combo => visible(combo.up('panel'))).forEach(combo => {
                    syncStore(combo.getStore(), current.services.map(item => ({ name: item.name })), row => row.name);
                    if (combo.getValue() !== selectedService) { combo.setValue(selectedService); }
                    combo.setHidden(current.services.length < 2);
                });
                const summary = me.down('#summary');
                if (visible(summary)) { summary.updateSnapshot(current); }
                rows('networkGrid', [{ id: 'net0', name: 'net0', bridge: current.bridge, hwaddr: current.hwaddr || '',
                    firewall: current.firewall, mtu: current.mtu, address: current.address, cidr: current.cidr,
                    ipv4mode: current.ipv4mode, pending: current.pending_network }]);
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
                    ]).concat(active.mounts.map((mount, index) => ({ key: 'Mount Point (' + index + ')', value: (mount.source || '') + ',mp=' + mount.target + (mount.read_only ? ',ro=1' : '') }))));
                    rows('dnsGrid', ['DNS servers', 'Search domains'].map(key => pendingRow(key, values, active.pending)));
                    rows('optionGrid', [{ key: 'Start at boot', value: Proxmox.Utils.format_boolean(current.onboot || 0) }, { key: 'Start/Shutdown order', value: current.startup || 'order=any' }, { key: 'Protection', value: Proxmox.Utils.format_boolean(current.protection) }, { key: 'Hookscript', value: current.hookscript || Proxmox.Utils.noneText }, pendingRow('Restart policy', values, active.pending), { key: 'Image', value: active.image }]);
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
                if (edit) {
                    edit.setDisabled(!selected || !networkEditable());
                    edit.setTooltip(current?.network_restored ? translate('Restored DCTs allow firewall changes only. Redeploy before changing the endpoint.') : undefined);
                }
                if (revert) { revert.setDisabled(!selected || !networkEditable() || !Object.keys(current?.pending_network || {}).length); }
            };
            const editNetwork = () => {
                if (!networkEditable() || !me.down('#networkGrid')?.getSelectionModel().getSelection()[0]) { return; }
                openEditor(me, 'network', () => {
                    const win = Ext.create('PVE.betterdocker.NetworkEdit', { url: url + '/network', method: 'PUT', nodename: info.node, restoredNetwork: current.network_restored, taskDone: reload,
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
            const editSettings = (group, selectedKey) => {
                const active = service();
                if (!active || !rowEditable(selectedKey, group) || locked()) { return; }
                if (selectedKey === 'Settings source') { return; }
                if (selectedKey === 'Project data disk') { editDataDisk(); return; }
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
                'DNS servers': ['dns'], 'Search domains': ['dns_search'], 'Restart policy': ['restart'] };
            const pendingRow = (key, values, pending) => {
                const format = values => ({
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
                { text: translate('Migrate'), itemId: 'migrateButton', bdScope: 'migrate', hidden: PVE.Utils.isStandaloneNode(), bdWrite: true, bdTemplateAllowed: true, bdNeedsStopped: true, iconCls: 'fa fa-send-o', handler: () => {
                        if (!guestActions(info, current, me.pendingTask).migrate.disabled) { Ext.create('PVE.betterdocker.Migrate', { autoShow: true, nodename: info.node,
                        projectId: identifier, vmid: info.vmid, vmname: info.name, haManaged: current?.ha?.managed, digest: current?.digest, listeners: { destroy: reload } }); }
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
                    { text: translate('Health'), dataIndex: 'health', renderer: value => Ext.htmlEncode(translate(value === 'none' ? 'No healthcheck' : value)) },
                    { text: translate('Restarts'), dataIndex: 'restarts', width: 85, flex: 0 },
                    { text: translate('Exit code'), dataIndex: 'exitcode', width: 85, flex: 0 },
                ], ['id', 'name', 'service', 'image', 'ports', 'state', 'health', 'restarts', 'exitcode'])],
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
                        } }]), { layout: 'fit' }),
                card('Resources', 'resources', 'cube', [properties('resourceGrid')],
                    [{ text: translate('Edit'), itemId: 'resourceEdit', disabled: true, handler: () => {
                        const grid = me.down('#resourceGrid');
                        const selected = grid.getSelectionModel().getSelection()[0];
                        if (selected) { editSettings('resources', selected.get('key')); }
                    } }, revertButton('resourceGrid'), { text: translate('Restore Compose'), itemId: 'restoreCompose', bdWrite: true, handler: () => {
                        const active = service(); if (!active || !active.overridden) { return; }
                        request({ url: url + '/reset-settings', method: 'POST', params: { service: active.name, digest: current.digest },
                            success: response => { trackTask(response, me); } });
                    } }, '->', selector()], { layout: 'fit' }),
                card('Network', 'network', 'exchange', [Object.assign(grid('networkGrid', [
                    { text: translate('Name'), dataIndex: 'name', width: 80, flex: 0 }, { text: translate('Bridge'), dataIndex: 'bridge' },
                    { text: translate('MAC address'), dataIndex: 'hwaddr' }, { text: translate('IPv4'), dataIndex: 'address' },
                    { text: translate('IPv4 mode'), dataIndex: 'mode' }, { text: gettext('Firewall'), dataIndex: 'firewall', renderer: renderNetwork }, { text: translate('MTU'), dataIndex: 'mtu' }].map(column => Object.assign(column, { renderer: renderNetwork })),
                    ['name', 'bridge', 'hwaddr', 'address', 'cidr', 'ipv4mode', 'mode', 'mtu', 'firewall', 'pending']), { flex: 1, listeners: { itemdblclick: editNetwork, selectionchange: updateNetworkActions, itemkeydown: (_, record, item, index, event) => editKey(editNetwork, record, event) } }),
                    Object.assign(grid('portGrid', [{ text: translate('Service'), dataIndex: 'service' }, { text: translate('IP address'), dataIndex: 'host_ip' },
                        { text: translate('Published port'), dataIndex: 'published' }, { text: translate('Container port'), dataIndex: 'target' },
                        { text: translate('Protocol'), dataIndex: 'protocol' }], ['service', 'host_ip', 'published', 'target', 'protocol']), { title: translate('Published ports'), flex: 1 })],
                    [{ text: translate('Edit'), itemId: 'networkEdit', disabled: true, handler: editNetwork },
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

    const openGuestAction = (info, action) => {
        const projectId = (info.realId || info.id).split('/')[1];
        const url = '/nodes/' + encodeURIComponent(info.node) + '/docker/' + projectId;
        request({ url: url + '/management', method: 'GET', success: response => {
            const state = response.result.data;
            const rules = guestActions(info, state)[action];
            if (rules.disabled) { if (rules.tooltip) { Ext.Msg.alert(translate('Error'), Ext.htmlEncode(rules.tooltip)); } return; }
            if (action === 'template') {
                if (state.template || state.status !== 'stopped') { return; }
                Ext.Msg.confirm(translate('Confirm'), guestConfirmation('template', info), answer => {
                    if (answer === 'yes') { request({ url: url + '/template', method: 'POST', params: { digest: state.digest },
                        success: result => { trackTask(result); } }); }
                });
                return;
            }
            if (action === 'migrate' && state.status !== 'stopped' && !state.ha?.managed) { return; }
            Ext.create(action === 'clone' ? 'PVE.betterdocker.StateRestore' : 'PVE.betterdocker.Migrate', {
                autoShow: true, nodename: info.node, projectId, vmid: info.vmid,
                guestname: state.name || info.name, vmname: state.name || info.name, isTemplate: state.template, digest: state.digest, haManaged: state.ha?.managed,
            });
        } });
    };

    Ext.define('PVE.betterdocker.CmdMenu', {
        extend: 'Ext.menu.Menu', showSeparator: false,
        initComponent: function () {
            const me = this, record = me.pveSelNode, info = record.data;
            const act = operation => {
                if (!guestActions(info, me.snapshotState)[operation].disabled) { dockerAction(info, operation, undefined, me.snapshotState); }
            };
            me.title = 'DCT ' + info.vmid;
            me.items = [
                { text: translate('Start'), itemId: 'start', iconCls: 'fa fa-fw fa-play', handler: () => act('start') },
                { text: translate('Shutdown'), itemId: 'shutdown', iconCls: 'fa fa-fw fa-power-off', handler: () => act('shutdown') },
                { text: translate('Stop'), itemId: 'force-stop', iconCls: 'fa fa-fw fa-stop', handler: () => act('force-stop') },
                { text: translate('Reboot'), itemId: 'reboot', iconCls: 'fa fa-fw fa-refresh', handler: () => act('reboot') },
                { xtype: 'menuseparator', itemId: 'actionsSeparator' },
                { text: translate('Clone'), itemId: 'clone', iconCls: 'fa fa-fw fa-clone', disabled: true,
                    handler: () => openGuestAction(info, 'clone') },
                { text: translate('Migrate'), itemId: 'migrate', iconCls: 'fa fa-fw fa-send-o', disabled: true,
                    hidden: PVE.Utils.isStandaloneNode(), handler: () => openGuestAction(info, 'migrate') },
                { text: translate('Convert to template'), itemId: 'template', iconCls: 'fa fa-fw fa-file-o', disabled: true,
                    handler: () => openGuestAction(info, 'template') },
                { xtype: 'menuseparator', itemId: 'consoleSeparator' },
                { text: translate('Console'), itemId: 'console', iconCls: 'fa fa-fw fa-terminal',
                    handler: () => { if (!guestActions(info, me.snapshotState).console.disabled) { openConsoleWindow(info); } } },

            ];
            if (info.template) {
                me.showSeparator = true;
                me.items = ['migrate', 'clone'].map(id => me.items.find(item => item.itemId === id));
            }
            const initial = guestActions(info);
            me.items.forEach(item => { if (initial[item.itemId]) { Object.assign(item, initial[item.itemId]); } });
            const separator = me.items.find(item => item.itemId === 'actionsSeparator');
            if (separator) { separator.hidden = initial.clone.hidden && initial.migrate.hidden && initial.template.hidden; }
            me.callParent();
            request({ url: '/nodes/' + encodeURIComponent(info.node) + '/docker/' + (info.realId || info.id).split('/')[1] + '/capabilities',
                method: 'GET', success: response => {
                    if (me.destroyed) { return; }
                    const state = response.result.data;
                    me.snapshotState = state;
                    const actions = guestActions(info, state);
                    ['start', 'shutdown', 'reboot', 'force-stop', 'clone', 'migrate', 'template', 'console'].forEach(action => {
                        const item = me.down('#' + action);
                        if (!item) { return; }
                        applyGuestAction(item, actions[action]);
                        // Older ExtJS wrappers expose setVisible instead of setHidden.
                        if (!item.setHidden && item.setVisible) { item.setVisible(!actions[action].hidden); }
                    });
                    if (me.getComponent) {
                        me.getComponent('actionsSeparator')?.setHidden(actions.clone.hidden && actions.migrate.hidden && actions.template.hidden);
                        me.getComponent('consoleSeparator')?.setHidden(actions.console.hidden);
                    }
                } });
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
                disabled: Proxmox.UserName !== 'root@pam' && !(Ext.state.Manager.get('GuiCap') || {}).vms?.['VM.Allocate'],
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
                    if (['tags', 'pool'].includes(view.id) && view.filterfn) {
                        const native = view.filterfn;
                        view.filterfn = record => record.data.type === 'docker' || native(record);
                    }
                    if (['tags', 'pool'].includes(view.id) && view.getFilterFn) {
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
            if (button) { button.setDisabled(!data || !(Proxmox.UserName === 'root@pam' || data.cap && data.cap.vms && data.cap.vms['VM.Allocate'])); }
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
                    margin: '3 5 0 0', baseCls: 'x-btn', disabled: Proxmox.UserName !== 'root@pam' && !(Ext.state.Manager.get('GuiCap') || {}).vms?.['VM.Allocate'],
                    handler: () => Ext.create('PVE.betterdocker.CreateWizard').show(),
                });
            } catch (error) { console.error('BetterDocker: cannot add toolbar button', error); }
        },
    });
})();
