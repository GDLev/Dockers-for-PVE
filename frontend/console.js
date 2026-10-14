Ext.define('PVE.betterdocker.Console', {
    extend: 'Ext.panel.Panel', alias: 'widget.pveBetterDockerConsole', layout: 'fit', bodyStyle: 'background:#000',
    initComponent: function () {
        const me = this;
        const disconnect = () => {
            if (me.socket) { me.socket.close(); me.socket = null; }
            clearInterval(me.ping);
            if (me.terminal) { me.terminal.dispose(); me.terminal = null; }
            me.connected = false; me.fit = null;
            me.generation = (me.generation || 0) + 1;
        };
        const resize = () => { if (me.fit) { me.fit.fit(); } };
        const connect = () => {
            const container = me.down('#consoleContainer').getValue();
            if (!container) { Ext.Msg.alert(translate('Console'), translate('Choose a running container.')); return; }
            disconnect();
            const generation = me.generation;
            request({ url: me.apiUrl + '/termproxy', method: 'POST',
                params: { container, shell: me.down('#shell').getValue() }, waitMsgTarget: me,
                success: response => {
                    if (me.destroyed || generation !== me.generation) { return; }
                    const data = response.result.data;
                    me.terminal = new Terminal({ cursorBlink: true, fontSize: 14, scrollback: 5000 });
                    me.fit = new FitAddon.FitAddon();
                    me.terminal.loadAddon(me.fit);
                    me.terminal.open(me.down('#terminal').getEl().dom);
                    resize();
                    const socket = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host +
                        '/api2/json' + me.apiUrl + '/vncwebsocket?port=' + data.port + '&vncticket=' + encodeURIComponent(data.ticket));
                    me.socket = socket;
                    socket.binaryType = 'arraybuffer';
                    socket.onopen = () => socket.send(data.user + ':' + data.ticket + '\n');
                    socket.onmessage = event => {
                        if (me.socket !== socket || !me.terminal) { return; }
                        const bytes = new Uint8Array(event.data);
                        if (!me.connected) {
                            if (bytes[0] !== 79 || bytes[1] !== 75) { socket.close(); return; }
                            me.connected = true;
                            me.terminal.write(bytes.slice(2));
                            me.terminal.focus();
                            socket.send('1:' + me.terminal.cols + ':' + me.terminal.rows + ':');
                        } else { me.terminal.write(bytes); }
                    };
                    socket.onclose = () => {
                        if (me.socket === socket && me.terminal) {
                            me.connected = false; clearInterval(me.ping);
                            me.terminal.write('\r\nConnection closed. Use Connect to open a new session.\r\n');
                        }
                    };
                    socket.onerror = () => { if (me.terminal) { me.terminal.write('\r\nConsole connection failed.\r\n'); } };
                    me.terminal.onData(value => {
                        if (me.connected) { socket.send('0:' + new TextEncoder().encode(value).length + ':' + value); }
                    });
                    me.terminal.onResize(size => { if (me.connected) { socket.send('1:' + size.cols + ':' + size.rows + ':'); } });
                    me.ping = setInterval(() => { if (socket.readyState === WebSocket.OPEN) { socket.send('2'); } }, 30000);
                },
            });
        };
        me.tbar = [
            { xtype: 'combobox', itemId: 'consoleContainer', fieldLabel: translate('Container'), labelWidth: 65, width: 350,
                queryMode: 'local', editable: false, displayField: 'name', valueField: 'id',
                store: { fields: ['id', 'name'], data: [] } },
            { xtype: 'combobox', itemId: 'shell', width: 110, value: '/bin/sh', editable: false,
                queryMode: 'local', store: [['/bin/sh', '/bin/sh'], ['/bin/bash', '/bin/bash']] },
            { text: translate('Connect'), itemId: 'consoleConnect', disabled: true, handler: connect }, { text: translate('Disconnect'), handler: disconnect },
        ];
        me.items = [{ xtype: 'component', itemId: 'terminal' }];
        me.callParent();
        const targets = () => request({ url: me.apiUrl + '/management', method: 'GET', success: response => {
            if (me.destroyed) { return; }
            const combo = me.down('#consoleContainer'), selected = combo.getValue();
            const allowed = Boolean(response.result.data.capabilities?.console);
            const containers = allowed ? response.result.data.containers.filter(container => container.state === 'running') : [];
            combo.setDisabled(!allowed);
            me.down('#shell').setDisabled(!allowed);
            me.down('#consoleConnect').setDisabled(!allowed || !containers.length);
            combo.getStore().loadData(containers);
            combo.setValue(containers.some(container => container.id === selected) ? selected : (containers[0] || {}).id);
        } });
        me.on('afterrender', targets, me, { single: true });
        me.on('activate', targets);
        me.on('resize', resize);
        me.on('beforedestroy', disconnect);
    },
});
