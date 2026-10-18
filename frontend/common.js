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
            '<p><a href="https://github.com/GDLev/dockers-for-pve" target="_blank" rel="noopener">' + Ext.htmlEncode(translate('Documentation')) + '</a></p>',
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
    tag: data.tag || '', rate: data.rate || '', link_down: Proxmox.Utils.format_boolean(data.link_down),
    mtu: data.mtu || translate('Same as bridge'), firewall: Proxmox.Utils.format_boolean(data.firewall),
})[key] ?? data[key] ?? '';
const renderNetwork = (value, meta, record) => {
    const key = meta.column?.dataIndex;
    const current = networkValue(record.data, key);
    const pending = record.data.pending;
    const requested = pending && networkValue(Object.assign({}, record.data, pending), key);
    if (pending?.removed) { return '<span style="color:darkorange;text-decoration:line-through">' + Ext.htmlEncode(current) + '</span>'; }
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
const dockerTaskLabels = { security: 'Security policy', imagepreview: 'Preview images', imageupdate: 'Update images', create: 'Create', update: 'Edit deployment', start: 'Start', stop: 'Shutdown',
    diskdelete: 'Remove disk', restart: 'Restart', shutdown: 'Shutdown', reboot: 'Reboot', forcestop: 'Stop', delete: 'Destroy', settings: 'Edit resources / options', network: 'Network', console: 'Console', snapshotcreate: 'Snapshot', snapshotrestore: 'Rollback', snapshotdelete: 'Delete Snapshot', restore: 'Restore', clone: 'Clone', template: 'Convert to template', migrate: 'Migrate' };
const dockerTaskDescriptions = { betterdocker: () => 'Docker - Deployment action' };
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
    const busy = Boolean(pendingTask || data.operation?.active || data.migration || data.image_update?.phase === 'applying');
    const ha = Boolean(state ? data.ha?.managed : data.hastate && data.hastate !== 'unmanaged');
    const override = !ha && !data.operation?.service && Boolean(data.capabilities?.overrule_shutdown);
    const pendingNetwork = Boolean(data.pending_network && Object.keys(data.pending_network).length);
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
            ...restriction(!allowed('migrate') || busy || pendingNetwork,
                pendingNetwork ? 'Apply or revert pending network changes before migrating the DCT.' : 'Operation in progress') },
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
Ext.define('PVE.betterdocker.GuestIDSelector', {
    extend: 'PVE.form.GuestIDSelector', alias: 'widget.pveBetterDockerGuestIDSelector',
    setRawValue: function (value) {
        const result = this.callParent(arguments);
        // The native next-ID loader sets only the raw value after initial validation.
        if (this.rendered && this.loadNextFreeID) { this.checkChange(); }
        return result;
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
