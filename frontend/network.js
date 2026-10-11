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
        me.interfaceName = me.interfaceName || 'net0';
        me.isCreate = Boolean(me.isCreate);
        me.items = [{ xtype: 'inputpanel',
            column1: [
                { xtype: 'displayfield', fieldLabel: translate('Name'), value: me.interfaceName },
                field('hwaddr', 'MAC address', '', { vtype: 'MacAddress', emptyText: 'auto' }),
                field('bridge', 'Bridge', '', { xtype: 'pveBetterDockerBridgeSelector', nodename: me.nodename, bridgeType: 'any_bridge', allowBlank: false }),
                field('tag', 'VLAN Tag', '', { xtype: 'numberfield', minValue: 1, maxValue: 4094, allowDecimals: false, emptyText: translate('No VLAN') }),
                { xtype: 'proxmoxcheckbox', name: 'firewall', fieldLabel: gettext('Firewall'), uncheckedValue: 0 },
                { xtype: 'proxmoxcheckbox', name: 'link_down', fieldLabel: gettext('Disconnect'), uncheckedValue: 0 },
            ],
            column2: [
                { xtype: 'radiogroup', fieldLabel: 'IPv4', columns: 3, items: [
                    { xtype: 'radiofield', name: 'ipv4mode', inputValue: 'static', boxLabel: translate('Static'), checked: true },
                    { xtype: 'radiofield', name: 'ipv4mode', inputValue: 'dhcp', boxLabel: 'DHCP' },
                    { xtype: 'radiofield', name: 'ipv4mode', inputValue: 'none', boxLabel: translate('None'), hidden: me.interfaceName === 'net0', disabled: me.interfaceName === 'net0' },
                ], listeners: { change: group => {
                    ['cidr', 'gw'].forEach(name => group.up('window').down('[name=' + name + ']')?.setDisabled(group.getValue().ipv4mode !== 'static'));
                } } },
                field('cidr', 'IPv4/CIDR', '', { vtype: 'IPCIDRAddress', allowBlank: false }),
                field('gw', 'Gateway (IPv4)', '', { vtype: 'IPAddress' }),
                { xtype: 'radiogroup', fieldLabel: 'IPv6', columns: 2, items: [
                    { xtype: 'radiofield', name: 'ipv6mode', inputValue: 'static', boxLabel: translate('Static') },
                    { xtype: 'radiofield', name: 'ipv6mode', inputValue: 'none', boxLabel: translate('None'), checked: true },
                ], listeners: { change: group => {
                    ['cidr6', 'gw6'].forEach(name => group.up('window').down('[name=' + name + ']')?.setDisabled(group.getValue().ipv6mode !== 'static'));
                } } },
                field('cidr6', 'IPv6/CIDR', '', { vtype: 'IPCIDRAddress', allowBlank: false, disabled: true }),
                field('gw6', 'Gateway (IPv6)', '', { vtype: 'IPAddress', disabled: true }),
            ],
            advancedColumn1: [
                field('mtu', 'MTU', '', { xtype: 'numberfield', minValue: 576, maxValue: 9216, allowDecimals: false, emptyText: translate('Same as bridge') }),
                field('rate', 'Rate limit (MB/s)', '', { xtype: 'numberfield', minValue: 0, maxValue: 100000, emptyText: translate('Unlimited') }),
            ],
            advancedColumn2: [field('ports', 'Published ports', '', { emptyText: 'web:8080/tcp', tooltip: translate('Unassigned ports use net0.') })],
            onGetValues: values => {
                values.net = me.interfaceName;
                if (values.ipv4mode !== 'static') { values.cidr = ''; values.gw = ''; }
                if (values.ipv6mode !== 'static') { values.cidr6 = ''; values.gw6 = ''; }
                ['mtu', 'tag', 'rate'].forEach(key => { values[key] = values[key] || 0; });
                return values;
            },
        }];
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
                const requested = Object.assign({ bridge: 'vmbr0', ipv4mode: 'static', ipv6mode: 'none' },
                    values.interfaces?.[me.interfaceName] || (me.interfaceName === 'net0' ? values : {}), values.pending?.interfaces?.[me.interfaceName] ||
                    (me.interfaceName === 'net0' ? values.pending : {}));
                ['mtu', 'tag', 'rate'].forEach(key => { if (!requested[key]) { requested[key] = ''; } });
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
