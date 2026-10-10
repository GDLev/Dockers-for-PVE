Ext.define('PVE.betterdocker.SnapshotEdit', {
    extend: 'PVE.window.Snapshot',
    load: function (options) {
        const success = options?.success;
        return this.callParent([Object.assign({}, options, { success: function (response, opts) {
            // Native load has already saved the digest. Keep it out of the
            // snapshot's configuration inventory.
            delete response.result.data.digest;
            return success?.(response, opts);
        } })]);
    },
    submit: function () {
        const me = this, form = me.formPanel.getForm();
        if (me.viewonly || !form.isValid()) { return; }
        const values = form.getValues();
        request({ url: me.url, method: me.isCreate ? 'POST' : 'PUT', waitMsgTarget: me,
            params: me.isCreate ? { action: 'create', snapname: values.snapname, description: values.description || '' } :
                { description: values.description || '', digest: me.digest },
            success: response => {
                if (me.isCreate) { trackTask(response, me.ownerPanel); task(response); }
                me.close();
            } });
    },
    initComponent: function () {
        const me = this;
        // Native Snapshot uses vmid in its API URL; DCT routes use the private project ID.
        me.vmid = me.projectId;
        me.type = 'docker';
        me.callParent();
        me.setTitle(me.isCreate ? translate('Create') + ': DCT ' + me.guestId + ' (' + me.vmname + ') ' + translate('Snapshot') :
            translate(me.viewonly ? 'View' : 'Edit') + ': ' + translate('Snapshot') + ' ' + me.snapname);
        const description = me.down('[name=description]');
        if (description) { description.setReadOnly(Boolean(me.viewonly)); description.maxLength = 4096; }
        const name = me.down('[name=snapname]');
        if (me.isCreate && name) { name.maxLength = 40; name.enforceMaxLength = true; }
        const summary = me.down('#summary');
        if (summary) { summary.getColumns().forEach(column => { column.renderer = Ext.htmlEncode; }); }
    },
});

Ext.define('PVE.betterdocker.Snapshots', {
    extend: 'PVE.guest.SnapshotTree', alias: 'widget.pveBetterDockerSnapshots', type: 'docker',
    // Reuse the native LXC tree, columns, toolbar, keyboard handling and editor.
    controller: {
        xclass: 'Ext.app.ViewController',
        init: function (view) {
            const me = this, vm = me.getViewModel(), info = view.ownerPanel.pveSelNode.data;
            vm.set('type', 'docker'); vm.set('nodename', info.node); vm.set('vmid', info.vmid); vm.set('vmname', info.name);
            me.load_task = new Ext.util.DelayedTask(me.reload, me);
            view.getStore().sorters.add({ property: 'order', direction: 'ASC' });
            view.updateActions();
            me.reload();
        },
        cancel: function () { this.load_task.cancel(); },
        select: function (_, records) {
            this.getViewModel().set('selected', records.length ? records[0].get('name') : '');
        },
        newSnapshot: function () { this.run_editor(false); },
        editSnapshot: function () { this.run_editor(true); },
        run_editor: function (edit) {
            const me = this, view = me.getView(), vm = me.getViewModel(), snapname = vm.get('selected');
            if (edit && (!snapname || snapname === 'current')) { return; }
            const info = view.ownerPanel.pveSelNode.data;
            if (!edit && !vm.get('snapshotAllowed')) { return; }
            const win = openEditor(view.ownerPanel, 'snapshot:' + (edit ? snapname : 'new'), () => Ext.create('PVE.betterdocker.SnapshotEdit', {
                ownerPanel: view.ownerPanel, nodename: info.node, projectId: (info.realId || info.id).split('/')[1], guestId: info.vmid, vmname: info.name,
                isCreate: !edit, viewonly: !vm.get('snapshotAllowed'), snapname: edit ? snapname : undefined,
                submitText: !edit ? translate('Take Snapshot') : undefined,
            }));
            me.mon(win, 'destroy', () => { me.reload(); view.ownerPanel.reloadDocker(); }, me);
        },
        rollback: function () { this.snapshotAction('restore'); },
        remove: function () { this.snapshotAction('delete'); },
        snapshotAction: function (action) {
            const me = this, view = me.getView(), snapname = me.getViewModel().get('selected');
            if (!snapname || snapname === 'current') { return; }
            request({ url: view.apiUrl + '/snapshot', method: 'POST', params: { action, snapname }, waitMsgTarget: view,
                callback: () => me.reload(), success: response => {
                    response.dockerTaskVisible = true;
                    trackTask(response, view.ownerPanel);
                    Ext.create('Proxmox.window.TaskProgress', { upid: response.result.data, autoShow: true });
                } });
        },
        reload: function () {
            const me = this, view = me.getView();
            if (me.destroyed || !visible(view) || me.loadingSnapshots) { return; }
            me.loadingSnapshots = true;
            request({ url: view.apiUrl + '/snapshots', method: 'GET', success: response => {
                me.loadingSnapshots = false;
                if (me.destroyed || !visible(view)) { return; }
                Proxmox.Utils.setErrorMask(view, false);
                const vm = me.getViewModel(), rows = response.result.data, current = rows.find(row => row.name === 'current');
                vm.set('running', Boolean(current?.running));
                const digest = current?.digest + ':' + Boolean(current?.running);
                if (digest !== me.old_digest) {
                    me.old_digest = digest;
                    keepGridPosition(view, () => {
                        const root = view.getRootNode(), nodes = new Map();
                        let structureChanged = false;
                        root.cascadeBy(node => { if (node !== root) { nodes.set(node.get('name'), node); } });
                        const wanted = new Set(rows.map(row => row.name));
                        rows.forEach(row => {
                            const values = Object.assign({}, row, { iconCls: row.name === 'current' ?
                                'fa fa-cube fa-fw x-fa-tree' : 'fa fa-fw fa-history x-fa-tree' });
                            let node = nodes.get(row.name);
                            if (node) { node.beginEdit(); node.set(values); node.endEdit(true); node.commit(); }
                            else { node = root.appendChild(Object.assign({ leaf: true }, values)); nodes.set(row.name, node); structureChanged = true; }
                        });
                        rows.forEach(row => {
                            const node = nodes.get(row.name), parent = nodes.get(row.parent) || root;
                            if (node.parentNode !== parent) { parent.appendChild(node); structureChanged = true; }
                        });
                        nodes.forEach((node, name) => { if (!wanted.has(name)) { node.remove(); structureChanged = true; } });
                        nodes.forEach((node, name) => {
                            if (!wanted.has(name)) { return; }
                            node.set({ leaf: !node.childNodes.length, expandable: false });
                            if (node.childNodes.length) { node.expand(); }
                        });
                        if (structureChanged) { view.getStore().sort(); }
                    });
                }
                view.updateActions(); me.load_task.delay(3000);
            }, failure: response => {
                me.loadingSnapshots = false;
                if (!me.destroyed && visible(view)) { Proxmox.Utils.setErrorMask(view, response.htmlStatus); me.load_task.delay(3000); }
            } });
        },
    },
    updateActions: function () {
        const me = this, vm = me.getViewModel(), owner = me.ownerPanel, data = owner.snapshotState;
        const unlocked = Boolean(data && !data.operation?.active && !data.migration && !owner.pendingTask);
        const supported = Boolean(data?.containers?.length && data.containers.length === data.expected && data.containers.every(item => ['created', 'exited', 'running'].includes(item.state)));
        vm.set('snapshotAllowed', unlocked && Boolean(data?.capabilities?.snapshot));
        vm.set('rollbackAllowed', unlocked && !data.template && !data.ha?.managed && Boolean(data?.capabilities?.rollback));
        vm.set('snapshotFeature', supported);
    },
    viewConfig: { preserveScrollOnRefresh: true, loadMask: false },
    loadSnapshots: function () { this.getController().reload(); },
    initComponent: function () {
        const me = this;
        me.callParent();
        me.on('activate', () => me.loadSnapshots());
        me.on('deactivate', () => me.getController().cancel());
        me.on('hide', () => me.getController().cancel());
        // The native rollback confirmation uses a VM identifier; retain its wording for DCT.
        const button = me.query('proxmoxButton').find(item => item.text === translate('Rollback'));
        if (button) { button.confirmMsg = function () {
            const info = me.ownerPanel.pveSelNode.data, record = me.getSelectionModel().getSelection()[0];
            return guestConfirmation('snapshotrestore', info) + Ext.htmlEncode(" '" + record.get('name') + "'? " + translate('Current state will be lost.'));
        }; }
    },
});
