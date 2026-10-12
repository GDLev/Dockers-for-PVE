Ext.define('PVE.betterdocker.SecurityEdit', {
    extend: 'Proxmox.window.Edit', width: 560, onlineHelp: 'chapter_system_administration', fieldDefaults: { labelWidth: 170 },
    initComponent: function () {
        const me = this, values = Object.assign({}, me.service.settings, me.service.pending);
        me.title = translate('Edit') + ': ' + translate('Security') + ' - ' + me.service.name;
        me.method = 'PUT';
        me.items = [{ xtype: 'inputpanel', items: [
            field('user', 'User', values.user || '', { emptyText: translate('Image default'), maxLength: 129 }),
            { xtype: 'proxmoxcheckbox', name: 'read_only', fieldLabel: translate('Read-only filesystem'), value: Boolean(values.read_only), uncheckedValue: 0 },
            field('cap_drop', 'Dropped capabilities', (values.cap_drop || []).join(', '), { emptyText: 'NET_RAW, SYS_ADMIN' }),
            { xtype: 'proxmoxcheckbox', name: 'no_new_privileges', fieldLabel: translate('No new privileges'), value: Boolean(values.no_new_privileges), uncheckedValue: 0 },
            field('apparmor', 'AppArmor profile', values.apparmor || '', { xtype: 'combobox', editable: false, queryMode: 'local',
                disabled: !me.security.supported, store: [['', translate('Docker default')],
                    ...me.security.profiles.filter(p => p !== 'unconfined').map(p => [p, p])],
                emptyText: translate(me.security.supported ? 'Docker default' : 'Not supported by host') }),
        ], fieldDefaults: { labelWidth: 170 } }];
        me.items[0].items.forEach(item => { if (item.fieldLabel) { item.labelWidth = 170; } });
        me.submit = function () {
            const form = me.formPanel.getForm();
            if (!form.isValid()) { return; }
            const changed = form.getValues();
            const settings = { user: changed.user, read_only: Number(changed.read_only) === 1,
                cap_drop: changed.cap_drop.split(/[ ,;]+/).filter(Boolean).map(v => v.toUpperCase().replace(/^CAP_/, '')),
                no_new_privileges: Number(changed.no_new_privileges) === 1 };
            if (me.security.supported) { settings.apparmor = changed.apparmor || ''; }
            request({ url: me.url, method: 'PUT', waitMsgTarget: me,
                params: { service: me.service.name, settings: JSON.stringify(settings), digest: me.digest },
                success: response => { trackTask(response, me.ownerPanel); me.close(); } });
        };
        me.callParent();
    },
});
