/* BetterDocker: a distinct GUI guest category backed by native unprivileged LXC. */
(function () {
    'use strict';
    if (!window.PVE || !PVE.lxc.CreateWizard || !PVE.StdWorkspace) {
        console.error('BetterDocker: unsupported Proxmox UI');
        return;
    }
    const marked = record => record && record.type === 'lxc' &&
        String(record.tags || '').split(';').includes('betterdocker');
    const originalIcon = PVE.Utils.get_object_icon_class;
    PVE.Utils.get_object_icon_class = function (type, record) {
        const icon = originalIcon.apply(this, arguments);
        return marked(record) && !record.template ? icon.replace('fa-cube', 'bd-docker-icon') : icon;
    };

    const parseJSON = (text, fallback) => text.trim() ? JSON.parse(text) : fallback;
    const field = (name, label, value, extra) => Object.assign({
        xtype: 'textfield', name: name, fieldLabel: label, value: value,
        labelWidth: 145, anchor: '100%',
    }, extra || {});
    const jsonValidator = (kind) => value => {
        try {
            const parsed = parseJSON(value, kind === 'array' ? [] : {});
            return (kind === 'array' ? Array.isArray(parsed) : parsed &&
                typeof parsed === 'object' && !Array.isArray(parsed)) || 'Nieprawidłowy typ JSON';
        } catch (_) { return 'Nieprawidłowy JSON'; }
    };
    const dockerSettings = values => ({
        mode: values.bd_mode,
        image: values.bd_image,
        ports: parseJSON(values.bd_ports || '', []),
        environment: parseJSON(values.bd_environment || '', {}),
        command: parseJSON(values.bd_command || '', []),
        compose: values.bd_compose || '',
    });

    Ext.define('PVE.betterdocker.CreateWizard', {
        extend: 'PVE.lxc.CreateWizard',
        subject: 'Docker',
        initComponent: function () {
            const me = this;
            const tabs = Ext.clone(PVE.lxc.CreateWizard.prototype.items);
            if (!Array.isArray(tabs) || !tabs[tabs.length - 1].onSubmit) {
                throw new Error('BetterDocker: incompatible LXC wizard');
            }
            // Preserve all native storage, CPU, memory, network, DNS and auth inputs.
            tabs[0].column1 = tabs[0].column1.filter(item =>
                !['features', 'unprivileged', 'ha-managed'].includes(item.name));
            tabs[0].column1.push({ xtype: 'displayfield', value: 'Docker w LXC: unprivileged, nesting i keyctl' });
            tabs.splice(tabs.length - 1, 0, {
                xtype: 'inputpanel', title: 'Docker Engine',
                items: [
                    field('bd_mode', 'Tryb wdrożenia', 'image', {
                        xtype: 'combobox', editable: false, queryMode: 'local', allowBlank: false,
                        store: [['image', 'Obraz Docker'], ['compose', 'Docker Compose'], ['engine', 'Tylko Docker Engine']],
                    }),
                    field('bd_image', 'Obraz Docker', 'nginx:stable', { allowBlank: false }),
                    field('bd_environment', 'Zmienne (JSON)', '{}', {
                        xtype: 'textarea', height: 95, validator: jsonValidator('object'),
                    }),
                    field('bd_command', 'Polecenie (JSON)', '[]', { validator: jsonValidator('array') }),
                    { xtype: 'displayfield', value: 'Wybierz standardowy szablon Debian 12 lub 13. Docker i Compose zostaną zainstalowane automatycznie.' },
                ],
            }, {
                xtype: 'inputpanel', title: 'Porty',
                items: [field('bd_ports', 'Mapowanie (JSON)', '[{"host":8080,"container":80,"protocol":"tcp"}]', {
                    xtype: 'textarea', height: 200, validator: jsonValidator('array'),
                }), { xtype: 'displayfield', value: 'Port host oznacza port na osobnym IP tej maszyny Docker. Obsługiwane: TCP/UDP, opcjonalne ip. W trybie Compose zdefiniuj ports w YAML.' }],
            }, {
                xtype: 'inputpanel', title: 'Docker Compose',
                items: [field('bd_compose', 'compose.yaml', '', { xtype: 'textarea', height: 270 }),
                    { xtype: 'displayfield', value: 'YAML jest wymagany w trybie Docker Compose. Możesz też zainstalować sam silnik lub uruchomić pojedynczy obraz.' }],
                validator: function () {
                    const values = this.up('window').getValues();
                    return values.bd_mode !== 'compose' || Boolean((values.bd_compose || '').trim());
                },
            });
            const confirm = tabs[tabs.length - 1];
            // Provisioning always starts the guest; the native optional checkbox is misleading here.
            confirm.dockedItems = [];
            confirm.listeners = {
                show: function () {
                    const values = this.up('window').getValues();
                    const rows = Object.keys(values)
                        .filter(key => !['password', 'confirmpw', 'bd_environment', 'bd_compose', 'bd_command', 'delete', 'tmplstorage'].includes(key))
                        .map(key => ({ key: key, value: values[key] }));
                    rows.push({ key: 'Docker', value: 'Automatyczna instalacja Engine + Compose; osobny interfejs sieciowy LXC' });
                    this.down('grid').getStore().loadData(rows);
                },
            };
            confirm.onSubmit = function () {
                const wizard = this.up('window');
                const values = wizard.getValues();
                let settings;
                try { settings = dockerSettings(values); } catch (_) {
                    Ext.Msg.alert('Docker', 'Sprawdź poprawność pól JSON.'); return;
                }
                const node = values.nodename;
                Object.keys(values).filter(key => key.startsWith('bd_') ||
                    ['nodename', 'tmplstorage', 'delete', 'ha-managed'].includes(key)).forEach(key => delete values[key]);
                if (!values.pool) { delete values.pool; }
                if (!values.password && values['ssh-public-keys']) { delete values.password; }
                values.docker = JSON.stringify(settings);
                Proxmox.Utils.API2Request({
                    url: '/nodes/' + encodeURIComponent(node) + '/docker',
                    method: 'POST', params: values, waitMsgTarget: wizard,
                    success: response => {
                        Ext.create('Proxmox.window.TaskViewer', { autoShow: true, upid: response.result.data });
                        wizard.close();
                    },
                    failure: response => Ext.Msg.alert('Docker', response.htmlStatus),
                });
            };
            me.items = tabs;
            me.callParent();
        },
    });
    Ext.override(PVE.StdWorkspace, {
        initComponent: function () {
            this.callParent();
            const user = this.down('#userinfo');
            if (!user || !user.ownerCt) { console.error('BetterDocker: toolbar not found'); return; }
            const toolbar = user.ownerCt;
            toolbar.insert(toolbar.items.indexOf(user), {
                xtype: 'button', text: 'Utwórz Docker', iconCls: 'fa bd-docker-icon',
                margin: '3 5 0 0', baseCls: 'x-btn', disabled: Proxmox.UserName !== 'root@pam',
                tooltip: 'Docker + Compose w osobnej maszynie z własnym IP (root@pam)',
                handler: () => Ext.create('PVE.betterdocker.CreateWizard').show(),
            });
        },
    });
})();
