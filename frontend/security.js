Ext.define('PVE.betterdocker.SecurityEdit', {
    extend: 'Proxmox.window.Edit', width: 560, onlineHelp: 'chapter_system_administration', fieldDefaults: { labelWidth: 170 },
    initComponent: function () {
        const me = this, values = Object.assign({}, me.service.settings, me.service.pending);
        me.title = translate('Edit') + ': ' + translate('Security') + ' - ' + me.service.name;
        me.method = 'PUT';
        me.items = [{ xtype: 'inputpanel', cls: 'bd-deployment-form', items: [
            field('user', 'User', values.user || '', { emptyText: translate('Image default'), maxLength: 129 }),
            { xtype: 'proxmoxcheckbox', name: 'read_only', fieldLabel: translate('Read-only filesystem'), value: Boolean(values.read_only), uncheckedValue: 0 },
            field('cap_drop', 'Dropped capabilities', (values.cap_drop || []).join(', '), { emptyText: 'NET_RAW, SYS_ADMIN' }),
            { xtype: 'proxmoxcheckbox', name: 'no_new_privileges', disabled: Boolean(me.security.mandatory), fieldLabel: translate('No new privileges'), value: Boolean(values.no_new_privileges), uncheckedValue: 0 },
            field('apparmor', 'AppArmor profile', values.apparmor || '', { xtype: 'combobox', editable: false, queryMode: 'local',
                disabled: !me.security.supported || Boolean(me.security.mandatory), store: [['', translate('Docker default')],
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
                no_new_privileges: me.security.mandatory || Number(changed.no_new_privileges) === 1 };
            if (me.security.supported && !me.security.mandatory) { settings.apparmor = changed.apparmor || ''; }
            request({ url: me.url, method: 'PUT', waitMsgTarget: me,
                params: { service: me.service.name, settings: JSON.stringify(settings), digest: me.digest },
                success: response => { trackTask(response, me.ownerPanel); me.close(); } });
        };
        me.callParent();
    },
});

Ext.define('PVE.betterdocker.PolicyEdit', {
    extend: 'Proxmox.window.Edit', width: 560,
    initComponent: function () {
        const me = this;
        me.title = translate('Edit') + ': ' + translate('Security policy');
        me.policyMessage = mode => {
            if (!me.policyData) { return true; }
            if (me.policyData.pending) { return translate('Apply or revert pending changes before changing security policy'); }
            if (me.policyData.running) { return translate('Stop the DCT before changing security policy'); }
            return mode === 'hardened' && me.policyData.incompatible?.length ? me.policyData.incompatible.map(Ext.htmlEncode).join('<br>') : true;
        };
        me.items = [{ xtype: 'inputpanel', cls: 'bd-deployment-form', items: [{ xtype: 'combobox', name: 'mode', fieldLabel: translate('Security policy'),
            editable: false, queryMode: 'local', disabled: true, allowBlank: false,
            validator: mode => me.policyMessage(mode),
            store: [['standard', translate('Standard')], ['hardened', translate('Hardened')]], value: me.policy }] }];
        const updatePreview = response => {
            if (me.destroyed) { return false; }
            me.policyData = response.result.data;
            const field = me.down('[name=mode]');
            field.setDisabled(false);
            field.validate();
            return true;
        };
        me.submit = function () {
            if (!me.policyData || !me.formPanel.getForm().isValid()) { return; }
            const mode = me.formPanel.getForm().getValues().mode;
            request({ url: me.url, method: 'GET', waitMsgTarget: me, success: response => {
                if (!updatePreview(response) || !me.formPanel.getForm().isValid()) { return; }
                request({ url: me.url, method: 'PUT', waitMsgTarget: me, params: { mode, digest: me.digest },
                    success: response => { trackTask(response, me.ownerPanel); me.close(); } });
            } });
        };
        me.callParent();
        me.on('afterrender', () => request({ url: me.url, method: 'GET', waitMsgTarget: me, success: updatePreview }), me, { single: true });
    },
});
