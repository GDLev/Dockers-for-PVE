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
