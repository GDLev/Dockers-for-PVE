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
    params: {},
    additionalItems: [{ xtype: 'proxmoxcheckbox', boxLabel: translate('Remove owned disks, volumes and project files'),
        checked: false, listeners: { change: (field, value) => {
            field.up('proxmoxSafeDestroy').params['destroy-owned-data'] = value ? 1 : 0;
        } } }],
    note: translate('External mounts are retained.'), showProgress: false,
    apiCallDone: (success, response) => { if (success) { trackTask(response, target); } },
});
const openConsoleWindow = info => Ext.create('Ext.window.Window', {
    autoShow: true, title: translate('Console') + ': DCT ' + (info.vmid || info.name),
    width: 900, height: 500, maximizable: true, constrain: true, layout: 'fit',
    items: [{ xtype: 'pveBetterDockerConsole',
        apiUrl: '/nodes/' + encodeURIComponent(info.node) + '/docker/' + (info.realId || info.id).split('/')[1] }],
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
        Ext.create(action === 'clone' ? 'PVE.betterdocker.StateRestore' : 'PVE.betterdocker.Migrate', {
            autoShow: true, nodename: info.node, projectId, vmid: info.vmid,
            guestname: state.name || info.name, vmname: state.name || info.name, isTemplate: state.template, digest: state.digest, running: Boolean(state.active_containers || ['running', 'partial'].includes(state.status)), haManaged: state.ha?.managed,
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
