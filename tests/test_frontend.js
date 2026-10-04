const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const definitions = {};
const overrides = new Map();
function callParent() {
    assert.ok(callParent.caller, 'ExtJS callParent must have a non-strict caller');
    this.initialized = true;
}
const tree = { typeDefaults: {}, prototype: { addChildSorted: (node, info) => info,
    getTypeOrder: type => ({ lxc: 0, qemu: 1, node: 2, storage: 4 })[type] ?? 9,
    nodeSortFn: (a, b) => a.data.vmid - b.data.vmid } };
const workspaceClass = { prototype: { initComponent() { this.initialized = true; }, setContent(comp) { this.content = comp; } } };
const nodeMenuClass = { prototype: { initComponent() { this.nativeInitialized = true; } } };
const context = { window: null, console,
    PVE: { node: { CmdMenu: nodeMenuClass }, panel: { Config: { prototype: { activateCard() {} } } }, window: { Wizard: {} }, StdWorkspace: workspaceClass, tree: { ResourceTree: tree },
        Utils: { get_object_icon_class: () => 'fa fa-cube', createCmdMenu: () => 'native-menu' } },
    Proxmox: { NodeName: 'pve', UserName: 'root@pam', Utils: { override_task_descriptions: descriptions => { context.taskDescriptions = descriptions; } } },
    Ext: { define: (name, definition) => { definitions[name] = definition; },
        override: (klass, override) => overrides.set(klass, override), htmlEncode: value => value },
};
context.window = context;
vm.runInNewContext(fs.readFileSync('frontend/betterdocker.js', 'utf8'), context);
const wizard = { callParent, down: () => null, query: () => [] };
definitions['PVE.betterdocker.CreateWizard'].initComponent.call(wizard);
assert(wizard.initialized);
assert.deepEqual(Array.from(wizard.items, item => item.title), ['Deployment', 'Network', 'Review']);
const fields = wizard.items[0].items;
assert.equal(fields.find(field => field.name === 'mode').value, 'compose');
assert.equal(fields.find(field => field.itemId === 'imageFields').hidden, true);
assert.equal(fields.find(field => field.itemId === 'imageFields').items[0].disabled, true);
assert(!JSON.stringify(wizard.items).includes('ostemplate'));
assert(!JSON.stringify(wizard.items).includes('pveLxc'));
const network = wizard.items[1];
assert.equal(network.column1.find(field => field.name === 'bridge').fieldLabel, 'Bridge');
assert(network.column1.find(field => field.name === 'hwaddr'));
assert(network.column1.find(field => field.name === 'mtu'));
const ipv4 = network.column2.find(field => field.xtype === 'radiogroup');
assert.equal(ipv4.items.find(item => item.inputValue === 'dhcp').checked, true);
assert.equal(network.column2.find(field => field.name === 'address').disabled, true);
let addressDisabled;
const addressField = { setDisabled: value => { addressDisabled = value; } };
ipv4.listeners.change({ up: () => ({ down: () => addressField }), getValue: () => ({ ipv4mode: 'static' }) });
assert.equal(addressDisabled, false);
ipv4.listeners.change({ up: () => ({ down: () => addressField }), getValue: () => ({ ipv4mode: 'dhcp' }) });
assert.equal(addressDisabled, true);
const inserted = [];
const user = { ownerCt: { items: { indexOf: () => 2 }, insert: (_, button) => inserted.push(button) } };
const workspace = { down: () => user };
overrides.get(workspaceClass).initComponent.call(workspace);
assert.equal(inserted[0].text, 'Create Docker');
assert(workspace.initialized);
overrides.get(workspaceClass).setContent.call(workspace, { pveSelNode: { data: { type: 'docker' } } });
assert.equal(workspace.content.xtype, 'pveBetterDockerConfig');
const groups = { composeFields: { query: () => [{ setDisabled: value => { groups.composeDisabled = value; } }], setHidden() {} },
    imageFields: { query: () => [{ setDisabled: value => { groups.imageDisabled = value; } }], setHidden() {} } };
const mode = fields.find(field => field.name === 'mode');
mode.listeners.change({ up: () => ({ down: id => groups[id.slice(1)] }), getValue: () => 'compose' });
assert.equal(groups.composeDisabled, false);
assert.equal(groups.imageDisabled, true);
assert(fs.readFileSync('frontend/docker.svg', 'utf8').includes('fill="#ffffff"'));
console.log('English wizard, LXC-style IPv4/DHCP controls, Docker resource routing and ExtJS checks passed');
const configPanel = { pveSelNode: { data: { id: 'docker/' + 'a'.repeat(32), node: 'pve', name: 'web' } },
    callParent, on() {}, down() { return null; } };
definitions['PVE.betterdocker.Config'].initComponent.call(configPanel);
assert.equal(definitions['PVE.betterdocker.Config'].extend, 'PVE.panel.Config');
assert.deepEqual(Array.from(configPanel.items, card => card.title),
    ['Summary', 'Console', 'Resources', 'Network', 'DNS', 'Options', 'Task History', 'Deployment', 'Logs']);
assert(configPanel.items.every(card => card.itemId && card.iconCls));
assert(configPanel.items.find(card => card.title === 'Resources').tbar.some(button => button.text === 'Edit'));
assert.equal(configPanel.items.find(card => card.title === 'Console').xtype, 'pveBetterDockerConsole');
assert(definitions['PVE.betterdocker.Console']);
console.log('Native vertical management navigation, resource editors and console configuration passed');
let consoleRequest;
let inputHandler;
let sizeHandler;
let disposed = false;
context.TextEncoder = TextEncoder;
context.location = { protocol: 'https:', host: 'pve.example:8006' };
context.setInterval = () => 1;
context.clearInterval = () => {};
context.Proxmox.Utils = { API2Request: options => { consoleRequest = options; } };
context.Terminal = class {
    constructor() { this.cols = 80; this.rows = 24; }
    loadAddon() {} open() {} focus() {} write() {}
    dispose() { disposed = true; }
    onData(handler) { inputHandler = handler; }
    onResize(handler) { sizeHandler = handler; }
};
context.FitAddon = { FitAddon: class { fit() {} } };
const sent = [];
let socket;
context.WebSocket = class {
    static OPEN = 1;
    constructor(url) { socket = this; this.url = url; this.readyState = 1; }
    send(value) { sent.push(value); }
    close() { this.readyState = 3; if (this.onclose) { this.onclose(); } }
};
const events = {};
const consolePanel = { apiUrl: '/nodes/pve/docker/' + 'a'.repeat(32), callParent,
    down(id) { return id === '#terminal' ? { getEl: () => ({ dom: {} }) } :
        { getValue: () => id === '#container' ? 'b'.repeat(64) : '/bin/sh' }; },
    on(event, callback) { events[event] = callback; } };
definitions['PVE.betterdocker.Console'].initComponent.call(consolePanel);
consolePanel.tbar.find(button => button.text === 'Connect').handler();
assert.equal(consoleRequest.method, 'POST');
assert.equal(consoleRequest.params.container, 'b'.repeat(64));
consoleRequest.success({ result: { data: { user: 'root@pam', ticket: 'ticket', port: 5901 } } });
assert(socket.url.startsWith('wss://pve.example:8006/api2/json/nodes/pve/docker/'));
socket.onopen();
assert.equal(sent[0], 'root@pam:ticket\n');
socket.onmessage({ data: new Uint8Array([79, 75]).buffer });
inputHandler('ą');
assert(sent.includes('0:2:ą'), 'terminal frames must use UTF-8 byte lengths');
sizeHandler({ cols: 100, rows: 30 });
assert(sent.includes('1:100:30:'));
events.beforedestroy();
assert(disposed);
assert.equal(socket.readyState, 3);
assert.equal(consolePanel.fit, null);
console.log('Authenticated terminal handshake, Unicode input, resizing and cleanup passed');

const treeOverride = overrides.get(tree);
assert.equal(treeOverride.getTypeOrder.call({}, 'docker'), 0.5);
const guests = [
    { data: { type: 'qemu', vmid: 1, id: 'qemu/1' } },
    { data: { type: 'docker', id: 'docker/z', name: 'zeta' } },
    { data: { type: 'lxc', vmid: 100, id: 'lxc/100' } },
    { data: { type: 'docker', id: 'docker/a', name: 'alpha' } },
];
guests.sort((a, b) => treeOverride.nodeSortFn.call({ 'group-guest-types': false }, a, b));
assert.deepEqual(guests.map(item => item.data.type), ['lxc', 'docker', 'docker', 'qemu']);
assert.equal(guests[1].data.name, 'alpha');
console.log('Consistent LXC → Docker → VM ordering passed');

const dockerConfig = definitions['PVE.betterdocker.Config'];
// Native Config.activateCard always calls Help.setOnlineHelp, whose lookup uses .replace().
// An undefined section used to abort initialization before native selectionchange reset layouts.
for (const card of configPanel.items) {
    const section = card.onlineHelp || dockerConfig.onlineHelp;
    assert.equal(typeof section, 'string', 'all Docker cards need a native help fallback');
    assert.doesNotThrow(() => section.replace(/_/g, '-'));
}
const activation = { suspendLayout: true };
context.PVE.panel.Config.prototype.activateCard = () => { throw new Error('card initialization failed'); };
assert.throws(() => dockerConfig.activateCard.call(activation, 'summary'), /card initialization failed/);
assert.equal(activation.suspendLayout, false, 'a failed Docker card must not leave layouts suspended');
context.PVE.panel.Config.prototype.activateCard = () => 'activated';
assert.equal(dockerConfig.activateCard.call(activation, 'resources'), 'activated');
console.log('Native Help fallback and layout recovery regression passed');

const nativeStore = { startUpdate() {}, stopUpdate() {}, destroy() {} };
context.Ext.create = () => nativeStore;
context.Ext.state = { Manager: { getProvider: () => ({}) } };
const summaryView = { apiUrl: '/nodes/pve/docker/' + 'a'.repeat(32), projectName: 'app',
    pveSelNode: configPanel.pveSelNode, callParent, on() {}, mon() {} };
definitions['PVE.betterdocker.Summary'].initComponent.call(summaryView);
assert.equal(summaryView.tbar[1].xtype, 'proxmoxRRDTypeSelector');
const summaryItems = summaryView.items[0].items;
assert.equal(summaryItems[0].layout.type, 'hbox');
assert.equal(summaryItems[0].items[1].xtype, 'pveBetterDockerNotes');
assert(summaryItems[0].items[0].items.some(item => item.xtype === 'pmxInfoWidget' && item.itemId === 'memoryValue'));
assert.equal(summaryItems.filter(item => item.xtype === 'proxmoxRRDChart').length, 4);
const resourceView = configPanel.items.find(card => card.itemId === 'resources');
assert.equal(resourceView.layout, 'fit');
assert.equal(resourceView.items.length, 1, 'mounts and limits share the native-style resource grid');
assert.equal(resourceView.items[0].hideHeaders, true);
console.log('Native status widgets, Notes, RRD charts and full-size resource grid passed');
const summaryFields = {};
context.Proxmox.Utils.format_duration_long = value => String(value);
context.Proxmox.Utils.render_cpu_usage = (value, cpus) => `${value * 100}% of ${cpus} CPU(s)`;
context.Proxmox.Utils.render_size_usage = (used, max) => `${used} / ${max}`;
const renderedSummary = { down(id) { return id === '#statusPanel' ? { setTitle: value => { summaryFields.title = value; } } :
    { updateValue: (text, usage) => { summaryFields[id] = { text, usage }; }, setPrintBar() {} }; } };
const summaryData = { name: 'app', node: 'pve', status: 'running', address: '192.0.2.32',
    containers: [{ state: 'running' }], services: [{ name: 'app', settings: { memory: 512, swap: 0, cpus: 1, cpuunits: 0,
        pids: 0, dns: [], dns_search: [], restart: 'always' }, mounts: [], ports: [] }],
    deployment: { mode: 'compose', compose: 'services: {}' }, metrics: { uptime: 60, cpu: 0.25, cpus: 1, mem: 128, maxmem: 512, swap: 32, maxswap: 128, disk: 100, maxdisk: 1000 } };
definitions['PVE.betterdocker.Summary'].updateSnapshot.call(renderedSummary, summaryData);
assert.equal(summaryFields['#memoryValue'].usage, 0.25);
assert.equal(summaryFields['#cpuValue'].usage, 0.25);
assert(summaryFields.title.includes('Uptime: 60'));
// Only Summary exists initially: inactive native navigation cards are created lazily.
const panelEvents = {};
const lazyPanel = { pveSelNode: configPanel.pveSelNode, callParent,
    on(event, callback) { panelEvents[event] = callback; }, query() { return []; },
    down(id) { return id === '#summary' ? { updateSnapshot() {} } : null; } };
context.Proxmox.Utils.setErrorMask = () => {};
definitions['PVE.betterdocker.Config'].initComponent.call(lazyPanel);
panelEvents.afterrender();
assert.doesNotThrow(() => consoleRequest.success({ result: { data: summaryData } }));
panelEvents.beforedestroy();
console.log('Summary values and reload with inactive cards absent passed');

assert.equal(summaryFields['#swapValue'].usage, 0.25);
assert.equal(summaryFields['#diskValue'].usage, 0.1);
console.log('SWAP usage and Bootdisk size status rows passed');

const createdWindows = [];
context.Ext.Msg = { confirm: (title, message, callback) => callback('yes') };
context.Ext.create = (name, options) => { createdWindows.push({ name, options }); return {}; };
const shutdownButton = configPanel.tbar.find(button => button.itemId === 'stopButton');
shutdownButton.handler();
assert.equal(consoleRequest.params.action, 'stop');
consoleRequest.success({ result: { data: 'UPID:stop' } });
shutdownButton.menu[0].handler();
assert.equal(consoleRequest.params.action, 'restart');
consoleRequest.success({ result: { data: 'UPID:restart' } });
assert.equal(createdWindows.length, 0, 'shutdown and restart must not open TaskViewer');
configPanel.tbar.find(button => button.itemId === 'startButton').handler();
consoleRequest.success({ result: { data: 'UPID:start' } });
assert.equal(createdWindows.length, 0, 'start must not open TaskViewer');
const clickedRecord = { data: { id: 'docker/' + 'c'.repeat(32), type: 'docker', node: 'pve-other', name: 'other-project', status: 'running' } };
const navigation = [];
const menuWorkspace = { selectById: id => navigation.push(id), query: () => [
    { pveSelNode: clickedRecord, selectById: card => navigation.push(card) }] };
const commandMenu = { pveSelNode: clickedRecord, workspace: menuWorkspace, callParent };
definitions['PVE.betterdocker.CmdMenu'].initComponent.call(commandMenu);
assert(commandMenu.items.find(item => item.text === 'Start').disabled);
commandMenu.items.find(item => item.text === 'Restart').handler();
assert.equal(consoleRequest.url, '/nodes/pve-other/docker/' + 'c'.repeat(32) + '/action');
const beforeRestart = createdWindows.length;
consoleRequest.success({ result: { data: 'UPID:restart-other' } });
assert.equal(createdWindows.length, beforeRestart);
commandMenu.items.find(item => item.text === 'Console').handler();
assert.deepEqual(navigation, [clickedRecord.data.id, 'console']);
context.Ext.tree = { View: class {} };
const treeView = new context.Ext.tree.View();
treeView.up = () => menuWorkspace;
let shownAt, menuArguments, suppressed = false;
const deferredMenuCleanup = [];
context.Ext.defer = (callback, delay) => {
    assert(delay > 0, 'Ext.defer with zero delay executes synchronously');
    deferredMenuCleanup.push(callback);
};
context.Ext.create = (name, options) => {
    menuArguments = { name, options };
    const menu = Object.assign({
        callParent,
        on(event, callback) { assert.equal(event, 'hide'); this.onHide = callback; },
        showAt(xy) { shownAt = xy; },
        destroy() {
            this.destroyed = true;
            this.items.forEach(item => { item.handler = null; });
            this.workspace = null;
        },
    }, options);
    definitions[name].initComponent.call(menu);
    return menu;
};
const openedMenu = context.PVE.Utils.createCmdMenu(treeView, clickedRecord, null, 0, { stopEvent() { suppressed = true; }, getXY() { return [100, 200]; } });
assert(suppressed);
assert.deepEqual(shownAt, [100, 200]);
assert.equal(menuArguments.name, 'PVE.betterdocker.CmdMenu');
assert.equal(menuArguments.options.pveSelNode, clickedRecord);
// Ext.menu.Item hides its parent before it reads and invokes its handler.
for (const label of ['Start', 'Shutdown', 'Restart', 'Console', 'Edit deployment', 'Remove']) {
    const record = { data: Object.assign({}, clickedRecord.data, { status: label === 'Start' ? 'stopped' : 'running' }) };
    const menu = context.PVE.Utils.createCmdMenu(treeView, record, null, 0, { stopEvent() {}, getXY() { return [100, 200]; } });
    const item = menu.items.find(entry => entry.text === label);
    assert.equal(item.disabled, false);
    menu.onHide();
    assert(!menu.destroyed, `${label}: hide must leave the item alive until its click handler runs`);
    assert.equal(typeof item.handler, 'function');
    let confirmed = false;
    context.Ext.Msg = { confirm(title, message, callback) {
        confirmed = true;
        if (label !== 'Remove') { callback('yes'); }
    } };
    item.handler();
    if (label === 'Remove') { assert(confirmed); }
    else if (label === 'Console') { assert.equal(navigation.at(-1), 'console'); }
    else if (label === 'Edit deployment') { assert.equal(consoleRequest.method, 'GET'); }
    else { assert.equal(consoleRequest.params.action, { Start: 'start', Shutdown: 'stop', Restart: 'restart' }[label]); }
    deferredMenuCleanup.shift()();
    assert(menu.destroyed, `${label}: the hidden menu must be cleaned up after the handler`);
}
openedMenu.onHide();
deferredMenuCleanup.shift()();
assert.equal(context.PVE.Utils.createCmdMenu(treeView, { data: { type: 'lxc' } }), 'native-menu');
context.Proxmox.UserName = 'user@pve';
const restrictedMenu = { pveSelNode: clickedRecord, workspace: menuWorkspace, callParent };
definitions['PVE.betterdocker.CmdMenu'].initComponent.call(restrictedMenu);
assert(restrictedMenu.items.filter(item => item.handler).every(item => item.disabled));
context.Proxmox.UserName = 'root@pam';
const createCT = { itemId: 'createct' };
let nodeMenuInsert;
const nodeMenu = { nodename: 'pve-selected', items: { indexOf: item => item === createCT ? 1 : -1 },
    getComponent: id => id === 'createct' ? createCT : undefined,
    insert: (index, item) => { nodeMenuInsert = { index, item }; } };
overrides.get(nodeMenuClass).initComponent.call(nodeMenu);
assert(nodeMenu.nativeInitialized);
assert.equal(nodeMenuInsert.index, 2, 'Create DCT must follow Create CT');
assert.equal(nodeMenuInsert.item.text, 'Create DCT');
assert.equal(nodeMenuInsert.item.disabled, false);
let wizardShown = false;
context.Ext.create = (name, options) => {
    assert.equal(name, 'PVE.betterdocker.CreateWizard');
    assert.equal(options.nodename, 'pve-selected');
    return { show() { wizardShown = true; } };
};
nodeMenuInsert.item.handler();
assert(wizardShown);
context.Proxmox.UserName = 'user@pve';
overrides.get(nodeMenuClass).initComponent.call(nodeMenu);
assert.equal(nodeMenuInsert.item.disabled, true);
context.Proxmox.UserName = 'root@pam';
console.log('Silent shutdown/restart and project-scoped native context menu passed');
console.log('Node context menu Create DCT ordering, permissions and selected-node wizard passed');

for (const [operation, label] of Object.entries({ create: 'Deploy', start: 'Start', stop: 'Shutdown', restart: 'Restart', delete: 'Remove', console: 'Console' })) {
    assert.equal(context.taskDescriptions['betterdocker' + operation]('', '101'), 'DCT 101 - ' + label);
}
assert(!context.taskDescriptions.betterdocker('', 'a'.repeat(32)).includes('a'.repeat(32)));
assert(!context.taskDescriptions.betterdockerconsole('', 'a'.repeat(32)).includes('a'.repeat(32)));
assert.equal(wizard.items[0].items.find(item => item.name === 'vmid').emptyText, 'Next available guest ID');
console.log('Readable DCT task descriptions and numeric ID wizard passed');

for (const [handler, operation] of [
    [shutdownButton.handler, 'stop'], [shutdownButton.menu[0].handler, 'restart'],
    [commandMenu.items.find(item => item.text === 'Shutdown').handler, 'stop'],
    [commandMenu.items.find(item => item.text === 'Restart').handler, 'restart'],
]) {
    let answer;
    const previousRequest = consoleRequest;
    context.Ext.Msg.confirm = (title, message, callback) => {
        assert.equal(title, 'Confirm');
        assert(message.includes(operation === 'stop' ? 'Shutdown' : 'Restart'));
        answer = callback;
    };
    handler();
    assert.equal(consoleRequest, previousRequest, 'No action before confirmation');
    answer('no');
    assert.equal(consoleRequest, previousRequest, 'Cancel must not send an action');
    handler();
    answer('yes');
    assert.equal(consoleRequest.params.action, operation);
    const windowsBefore = createdWindows.length;
    consoleRequest.success({ result: { data: 'UPID:confirmed-action' } });
    assert.equal(createdWindows.length, windowsBefore, 'Confirmed action must not open task output');
}
context.Ext.Msg.confirm = () => { throw new Error('Start must not ask for confirmation'); };
configPanel.tbar.find(button => button.itemId === 'startButton').handler();
assert.equal(consoleRequest.params.action, 'start');
console.log('Shutdown/restart confirmation and cancellation in toolbar and context menu passed');
