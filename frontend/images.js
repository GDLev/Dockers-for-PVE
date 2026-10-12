Ext.define('PVE.betterdocker.ImageUpdate', {
    extend: 'Proxmox.window.Edit', isCreate: true, width: 760, height: 440, resizable: true,
    onlineHelp: 'chapter_system_administration', fieldDefaults: { labelWidth: 150 },
    initComponent: function () {
        const me = this;
        let plan;
        me.title = translate('Update images');
        me.url = me.apiUrl + '/images'; me.method = 'POST'; me.submitText = translate('Update');
        me.items = [{ xtype: 'grid', itemId: 'images', height: 190,
            selModel: { selType: 'checkboxmodel', mode: 'MULTI' },
            listeners: { selectionchange: function (_, records) { me.down('#preview')?.setDisabled(!records.length); } },
            plugins: [{ ptype: 'cellediting', clicksToEdit: 1 }],
            store: { fields: ['service', 'image', 'current', 'candidate'], data: me.services.map(s => ({ service: s.name, image: s.image_reference || s.image })) },
            columns: [{ text: translate('Service'), dataIndex: 'service', width: 120, renderer: Ext.htmlEncode },
                { text: translate('Image'), dataIndex: 'image', flex: 2, renderer: Ext.htmlEncode, editor: { xtype: 'textfield', allowBlank: false } },
                { text: translate('Current image'), dataIndex: 'current', flex: 1, renderer: Ext.htmlEncode },
                { text: translate('New image'), dataIndex: 'candidate', flex: 1, renderer: Ext.htmlEncode }],
        }, { xtype: 'button', itemId: 'preview', disabled: true, text: translate('Preview'), margin: '5 0', handler: function () {
            const grid = me.down('#images'), selected = grid.getSelectionModel().getSelection();
            if (!selected.length || me.busy) { return; }
            const release = response => {
                if (me.destroyed) { return; }
                me.busy = false; grid.setDisabled(false); this.setDisabled(false);
                if (response) { Ext.Msg.alert(translate('Error'), response.htmlStatus); }
            };
            me.busy = true; grid.setDisabled(true); this.setDisabled(true); me.lookup('submitbutton')?.setDisabled(true);
            request({ url: me.apiUrl + '/images/preview', method: 'POST', params: { digest: me.digest,
                images: JSON.stringify(Object.fromEntries(selected.map(r => [r.get('service'), r.get('image')]))) },
                success: response => {
                    const upid = response.result.data;
                    const node = me.nodename;
                    const poll = () => {
                        if (me.destroyed) { return; }
                        request({ url: '/nodes/' + encodeURIComponent(node) + '/tasks/' + encodeURIComponent(upid) + '/status', method: 'GET',
                            success: status => {
                                if (status.result.data.status !== 'stopped') { Ext.defer(poll, 1000); return; }
                                if (status.result.data.exitstatus !== 'OK') { release(); task(response); return; }
                                request({ url: me.apiUrl + '/images', method: 'GET', success: result => {
                                    if (me.destroyed) { return; }
                                    me.busy = false; plan = result.result.data;
                                    grid.getStore().loadData(plan.changes.map(row => Object.assign({}, row, { current: row.current.join(', ') })));
                                    grid.setDisabled(true); this.setDisabled(true); me.formPanel.getForm().isValid();
                                    me.lookup('submitbutton')?.setDisabled(!me.stopped || !plan.changes.some(row => row.changed));
                                }, failure: release });
                            }, failure: release });
                    };
                    poll();
                }, failure: release });
        } }, field('recovery', 'Recovery snapshot', 'image-update-' + Date.now().toString(36), { allowBlank: false, maxLength: 40, regex: /^[A-Za-z][A-Za-z0-9_-]{0,39}$/ }),
        { xtype: 'displayfield', value: translate('Updating images requires a stopped DCT. Roll back the recovery snapshot to restore containers and data.') }];
        me.submit = function () {
            if (!plan || !me.stopped || me.busy || !me.formPanel.getForm().isValid()) { return; }
            request({ url: me.apiUrl + '/images', method: 'POST', waitMsgTarget: me,
                params: { token: plan.token, recovery: me.formPanel.getForm().getValues().recovery, digest: plan.digest },
                success: response => { trackTask(response, me.ownerPanel); me.close(); } });
        };
        me.callParent();
        const syncSubmit = () => me.lookup('submitbutton')?.setDisabled(!plan || !me.stopped || me.busy || !plan.changes.some(row => row.changed) || !me.formPanel.getForm().isValid());
        me.formPanel.getForm().on('dirtychange', syncSubmit);
        me.formPanel.getForm().on('validitychange', syncSubmit);
        me.lookup('submitbutton')?.setDisabled(true);
    },
});
