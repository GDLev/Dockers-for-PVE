PVE.tree.ResourceTree.typeDefaults.docker = { iconCls: 'fa bd-docker-icon', text: translate('Docker') };
const originalIcon = PVE.Utils.get_object_icon_class;
PVE.Utils.get_object_icon_class = function (type, record) {
    if (type === 'docker') {
        if (record.template) {
            return originalIcon.call(this, 'lxc', record).replace(/\blxc\b/g, 'bd-docker-template-icon');
        }
        return 'fa bd-docker-icon ' + (['running', 'stopped'].includes(record.status) ? record.status : 'unknown');
    }
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
