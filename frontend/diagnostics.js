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
                    { key: 'Runtime mode', value: info.runtime || 'rootful' }, { key: 'Runtime service', value: info.runtime_health || translate('Unavailable') },
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
