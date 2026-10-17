function creationResourcePanels(wizard) {
    const defaults = { cpus: 1, memory: 512, swap: 512 };
    wizard.creationResources = Object.create(null);
    const selectedValues = service => Object.assign({}, wizard.creationPlan?.find(row => row.service === service)?.resources,
        wizard.creationResources[service]);
    const selectService = (panel, service) => {
        if (!service) { return; }
        panel.creationService = service;
        panel.loadingResources = true;
        const values = selectedValues(service);
        panel.query('[creationResource]').forEach(input => input.setValue(values[input.creationResource] || 0));
        panel.loadingResources = false;
    };
    const loadPanel = panel => {
        const selector = panel.down('[itemId=creationService]');
        const plan = wizard.creationPlan || [];
        selector.getStore().loadData(plan.map(row => ({ name: row.service })));
        selector.setHidden(plan.length < 2);
        selector.setValue(plan.some(row => row.service === panel.creationService) ? panel.creationService : plan[0]?.service);
        selectService(panel, selector.getValue());
        const disk = panel.down('[creationResource=disk]');
        if (disk) { disk.setDisabled(!plan[0]?.rootfs_quota); disk.setEmptyText(plan[0]?.rootfs_quota ? '' : translate('Not supported by host')); }
    };
    const resource = (key, label, maximum, extra = {}) => field('creation_' + key, label, defaults[key] ?? 0, Object.assign({
        xtype: 'numberfield', creationResource: key, submitValue: false, minValue: 0, maxValue: maximum,
        allowDecimals: false, allowBlank: false,
        listeners: { change: function (input, value) {
            const panel = input.up('inputpanel'), service = panel.creationService;
            if (!service || panel.loadingResources) { return; }
            const changes = wizard.creationResources[service] ||= {};
            if (value === (wizard.creationPlan.find(row => row.service === service).resources[key] || 0)) { delete changes[key]; }
            else { changes[key] = value; }
        } },
    }, extra));
    const selector = () => ({ xtype: 'combobox', itemId: 'creationService', fieldLabel: translate('Service'),
        submitValue: false, editable: false, queryMode: 'local', displayField: 'name', valueField: 'name',
        store: { fields: ['name'], data: [] },
        listeners: { change: (combo, value) => selectService(combo.up('inputpanel'), value) } });
    const diskFields = enabled => {
        const panel = wizard.down('#creationDisks');
        if (!panel) { return; }
        ['creation_storage', 'creation_size'].forEach(name => panel.down('[name=' + name + ']').setDisabled(!enabled));
        const target = panel.down('[name=creation_target]');
        const image = wizard.getValues().mode === 'image';
        target.setHidden(!image); target.setDisabled(!enabled || !image);
    };
    wizard.creationOptions = () => {
        const options = {};
        const resources = Object.fromEntries(Object.entries(wizard.creationResources).filter(([, values]) => Object.keys(values).length));
        if (Object.keys(resources).length) { options.resources = resources; }
        const panel = wizard.down('#creationDisks');
        if (panel?.down('[name=creation_disk_enabled]')?.getValue()) {
            const disk = { storage: panel.down('[name=creation_storage]').getValue(), size_mib: Math.round(panel.down('[name=creation_size]').getValue() * 1024) };
            if (wizard.getValues().mode === 'image') { disk.target = panel.down('[name=creation_target]').getValue(); }
            options.data_disk = disk;
        }
        return options;
    };
    const loadPlan = panel => {
        const values = wizard.getValues();
        const source = deployment(Object.assign({}, values, { ipv4mode: 'none', address: '', interfaces: undefined }));
        const key = JSON.stringify([values.nodename, values.pool, source]);
        if (wizard.creationPlanKey === key && wizard.creationPlan) { loadPanel(panel); diskFields(wizard.down('#creationDisks').down('[name=creation_disk_enabled]').getValue()); return; }
        const token = wizard.creationPlanToken = {};
        const disks = wizard.down('#creationDisks');
        disks.down('[name=creation_ready]').setValue('');
        Proxmox.Utils.setErrorMask(panel, false);
        request({ url: '/nodes/' + encodeURIComponent(values.nodename) + '/docker', method: 'PUT',
            params: Object.assign({ deployment: JSON.stringify(source), resources_only: 1 }, values.pool ? { pool: values.pool } : {}), waitMsgTarget: panel,
            failure: response => { if (!wizard.destroyed && wizard.creationPlanToken === token) { Proxmox.Utils.setErrorMask(panel, response.htmlStatus); } },
            success: response => {
                if (wizard.destroyed || wizard.creationPlanToken !== token) { return; }
                wizard.creationPlan = response.result.data; wizard.creationPlanKey = key; wizard.creationResources = Object.create(null);
                for (const row of wizard.creationPlan) {
                    const changes = wizard.creationResources[row.service] = {};
                    for (const key of row.resource_defaults || []) { changes[key] = defaults[key]; }
                }
                loadPanel(disks);
                if (panel !== disks) { loadPanel(panel); }
                diskFields(disks.down('[name=creation_disk_enabled]').getValue());
                disks.down('[name=creation_storage]').setNodename(values.nodename);
                disks.down('[name=creation_ready]').setValue('ready');
            } });
    };
    return [{ xtype: 'inputpanel', cls: 'bd-deployment-form', itemId: 'creationDisks', title: translate('Disks'), onlineHelp: 'chapter_system_administration',
        column1: [
            { xtype: 'textfield', name: 'creation_ready', hidden: true, allowBlank: false, submitValue: false },
            { xtype: 'proxmoxcheckbox', name: 'creation_disk_enabled', fieldLabel: translate('Project data disk'), submitValue: false,
                listeners: { change: (_, enabled) => diskFields(enabled) } },
            { xtype: 'pveStorageSelector', name: 'creation_storage', fieldLabel: gettext('Storage'), storageContent: 'rootdir',
                nodename: wizard.nodename || Proxmox.NodeName, storageType: ['dir', 'lvmthin', 'zfspool'], allowBlank: false, disabled: true, submitValue: false },
            field('creation_size', 'Disk size (GiB)', 8, { xtype: 'numberfield', minValue: 0.25, maxValue: 1024,
                allowDecimals: true, decimalPrecision: 2, allowBlank: false, disabled: true, submitValue: false }),
            field('creation_target', 'Mount point', '/data', { allowBlank: false, disabled: true, hidden: true, submitValue: false,
                validator: value => value.startsWith('/') && value !== '/' && !value.split('/').includes('..') || translate('Invalid mount point') }),
        ], column2: [selector()], advancedColumn2: [resource('disk', 'Container disk (MiB)', 1048576, { disabled: true })],
        listeners: { show: loadPlan },
    }, {
        xtype: 'inputpanel', cls: 'bd-deployment-form', itemId: 'creationCPU', title: translate('CPU'), onlineHelp: 'chapter_system_administration',
        column1: [resource('cpus', 'CPU limit', 8192, { emptyText: '1', allowDecimals: true, decimalPrecision: 3, step: 1 })],
        column2: [selector()], advancedColumn1: [resource('cpuunits', 'CPU units', 262144)], listeners: { show: loadPlan },
    }, {
        xtype: 'inputpanel', cls: 'bd-deployment-form', itemId: 'creationMemory', title: translate('Memory'), onlineHelp: 'chapter_system_administration',
        column1: [resource('memory', 'Memory (MiB)', 1048576, { step: 32 }), resource('swap', 'Swap (MiB)', 1048576, { minValue: -1, step: 32 })],
        column2: [selector()], listeners: { show: loadPlan },
    }];
}
function creationResourceReview(wizard) {
    const options = wizard.creationOptions(), rows = [];
    if (options.data_disk) {
        rows.push(['Storage', options.data_disk.storage], ['Project data disk', Proxmox.Utils.format_size(options.data_disk.size_mib * 1048576)]);
        if (options.data_disk.target) { rows.push(['Mount point', options.data_disk.target]); }
    }
    for (const item of wizard.creationPlan || []) {
        const values = Object.assign({}, item.resources, options.resources?.[item.service]);
        rows.push([item.service + ' / ' + translate('CPU limit'), values.cpus || translate('Unlimited')],
            [item.service + ' / ' + translate('Memory'), values.memory ? Proxmox.Utils.format_size(values.memory * 1048576) : translate('Unlimited')],
            [item.service + ' / ' + translate('Swap'), values.swap === -1 ? translate('Unlimited') : Proxmox.Utils.format_size(values.swap * 1048576)]);
        if (values.cpuunits) { rows.push([item.service + ' / ' + translate('CPU units'), values.cpuunits]); }
        if (values.disk) { rows.push([item.service + ' / ' + translate('Container disk limit'), Proxmox.Utils.format_size(values.disk * 1048576)]); }
    }
    return rows;
}
