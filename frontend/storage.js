Ext.define('PVE.betterdocker.StorageVolumes', {
    extend: 'Ext.grid.GridPanel', alias: 'widget.pveBetterDockerStorageVolumes',
    onlineHelp: 'chapter_storage',
    initComponent: function () {
        const me = this, node = me.pveSelNode.data.node, storage = me.pveSelNode.data.storage;
        me.store = Ext.create('Ext.data.Store', {
            fields: ['volid', 'vmid', 'dct_name', 'dct_status', 'dct_id', 'dct_node', 'format', 'size'],
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
        const openGuest = record => {
            if (!record?.get('dct_id') || !record.get('dct_node')) { return; }
            const workspace = me.up('pveStdWorkspace');
            workspace.selectById('docker/' + record.get('dct_id'));
        };
        const remove = record => {
            if (!record || !['unused', 'retained'].includes(record.get('dct_status'))) { return; }
            Ext.create('Proxmox.window.SafeDestroy', {
                autoShow: true, title: Ext.String.format(gettext("Destroy '{0}'"), record.get('volid')),
                item: { id: record.get('vmid'), formattedIdentifier: record.get('volid') },
                taskName: 'betterdockerdiskdelete', showProgress: true,
                url: '/nodes/' + encodeURIComponent(node) + '/docker/' + record.get('dct_id') + '/volumes/' + record.get('vmid'),
                params: { volume: record.get('volid') },
                taskDone: () => me.store.load(),
            });
        };
        me.tbar = [
            { text: gettext('Remove'), itemId: 'removeVolume', disabled: true,
                handler: () => remove(me.getSelection()[0]) },
            { text: gettext('Show'), itemId: 'showGuest', disabled: true,
                handler: () => openGuest(me.getSelection()[0]) },
            '->', { text: gettext('Reload'), iconCls: 'fa fa-refresh', handler: () => me.store.load() },
        ];
        me.listeners = {
            itemdblclick: (_, record) => openGuest(record),
            selectionchange: (_, records) => {
                const record = records[0], caps = Ext.state.Manager.get('GuiCap') || {};
                const diskPermission = Proxmox.UserName === 'root@pam' ||
                    (record?.get('dct_status') === 'retained' ? caps.nodes?.['Sys.Modify'] : caps.vms?.['VM.Config.Disk']);
                const storagePermission = Proxmox.UserName === 'root@pam' || caps.storage?.['Datastore.AllocateSpace'];
                me.down('#removeVolume').setDisabled(!record || !diskPermission || !storagePermission ||
                    !['unused', 'retained'].includes(record.get('dct_status')));
                me.down('#showGuest').setDisabled(!record?.get('dct_id') || !record.get('dct_node'));
            },
        };
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
