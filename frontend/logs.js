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
