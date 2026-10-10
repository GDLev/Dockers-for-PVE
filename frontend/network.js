Ext.define('PVE.betterdocker.BridgeSelector', {
    extend: 'PVE.form.BridgeSelector', alias: 'widget.pveBetterDockerBridgeSelector', bridgeType: 'any_bridge',
    initComponent: function () {
        this.callParent();
        this.getStore().addFilter(record => ['bridge', 'vnet'].includes(record.get('type')));
    },
});

Ext.define('PVE.betterdocker.FirewallOptions', {
    extend: 'PVE.FirewallOptions', alias: 'widget.pveBetterDockerFirewallOptions',
    add_boolean_row: function (name) {
        this.rows.status = { header: gettext('Status'), required: true, defaultValue: 'unknown',
            renderer: value => gettext({ active: 'Active', inactive: 'Inactive', error: 'Error', unknown: 'Unknown' }[value] || 'Unknown') };
        if (['dhcp', 'ndp', 'radv', 'macfilter', 'ipfilter'].includes(name)) { return; }
        return this.callParent(arguments);
    },
});

Ext.define('PVE.betterdocker.NetworkEdit', {
    extend: 'Proxmox.window.Edit', subject: translate('Network Device'), showTaskViewer: false,
    initComponent: function () {
        const me = this;
        me.items = [{ xtype: 'inputpanel',
            column1: [
                { xtype: 'displayfield', fieldLabel: translate('Name'), value: 'net0' },
                field('hwaddr', 'MAC address', '', { vtype: 'MacAddress', emptyText: 'auto' }),
                field('bridge', 'Bridge', '', { xtype: 'pveBetterDockerBridgeSelector', nodename: me.nodename,
                    bridgeType: 'any_bridge', allowBlank: false }),
            ],
            column2: [
                { xtype: 'radiogroup', fieldLabel: 'IPv4', columns: 2, items: [
                    { xtype: 'radiofield', name: 'ipv4mode', inputValue: 'static', boxLabel: translate('Static'), checked: true },
                    { xtype: 'radiofield', name: 'ipv4mode', inputValue: 'dhcp', boxLabel: 'DHCP' },
                ], listeners: { change: function (group) {
                    const address = group.up('window').down('[name=cidr]');
                    if (address) { address.setDisabled(me.restoredNetwork || group.getValue().ipv4mode !== 'static'); }
                } } },
                field('cidr', 'IPv4/CIDR', '', { vtype: 'IPCIDRAddress', allowBlank: false }),
                { xtype: 'proxmoxcheckbox', name: 'firewall', fieldLabel: gettext('Firewall'), uncheckedValue: 0 },
            ],
            advancedColumn1: [field('mtu', 'MTU', '', { xtype: 'numberfield', minValue: 576, maxValue: 9216,
                allowDecimals: false, emptyText: translate('Same as bridge') })],
            onGetValues: function (values) {
                if (values.ipv4mode === 'dhcp') { values.cidr = ''; }
                if (me.restoredNetwork) { return { firewall: values.firewall }; }
                values.mtu = values.mtu || 0;
                return values;
            },
        }];
        if (me.restoredNetwork) {
            const panel = me.items[0];
            [...panel.column1, ...panel.column2, ...panel.advancedColumn1].filter(item => item.name !== 'firewall').forEach(item => { item.disabled = true; });
        }
        me.callParent();
        // Native Edit.load touches its form before invoking success. A
        // delayed response must not touch a network editor closed with Escape.
        let closed = false;
        me.on('beforedestroy', () => { closed = true; });
        me.setLoading(gettext('Please wait...'));
        Proxmox.Utils.API2Request({ url: me.url, method: 'GET',
            success: function (response) {
                if (closed) { return; }
                me.setLoading(false);
                me.formPanel.getForm().clearInvalid();
                const values = response.result.data;
                const requested = Object.assign({}, values, values.pending);
                if (!requested.mtu) { requested.mtu = ''; }
                me.setValues(requested);
                me.digest = values.digest;
                me.query('radiofield').forEach(item => item.resetOriginalValue());
            },
            failure: function (response) {
                if (closed) { return; }
                me.setLoading(false);
                Ext.Msg.alert(gettext('Error'), response.htmlStatus, () => { if (!closed) { me.close(); } });
            },
        });
    },
});
