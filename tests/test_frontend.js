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
const viewSelectorClass = { prototype: { initComponent() { this.nativeInitialized = true; } } };
const workspaceClass = { prototype: { initComponent() { this.initialized = true; }, setContent(comp) { this.content = comp; }, selectById(id) { return id; } } };
const nodeMenuClass = { prototype: { initComponent() { this.nativeInitialized = true; } } };
const nodeConfigClass = { prototype: { initComponent() { this.nativeInitialized = true; } } };
const storageBrowserClass = { prototype: { initComponent() { this.nativeInitialized = true; } } };
const storageImageClass = { prototype: { initComponent() { this.nativeInitialized = true; } } };
const context = { window: null, console, gettext: value => value,
    PVE: { form: { VMSelector: { prototype: { initComponent() {} } }, ViewSelector: viewSelectorClass }, storage: { Browser: storageBrowserClass, ImageView: storageImageClass }, node: { CmdMenu: nodeMenuClass, Config: nodeConfigClass }, panel: { Config: { prototype: { activateCard() {}, initComponent() { this.nativeInitialized = true; } } } }, window: { Wizard: {} }, StdWorkspace: workspaceClass, tree: { ResourceTree: tree },
        Utils: { format_ha: value => value.managed ? value.state || 'none' : 'none', isStandaloneNode: () => false, get_object_icon_class: () => 'fa fa-cube', createCmdMenu: () => 'native-menu' } },
    Proxmox: { NodeName: 'pve', UserName: 'root@pam', Utils: { format_boolean: value => value ? 'Yes' : 'No', override_task_descriptions: descriptions => { context.taskDescriptions = descriptions; } } },
    Ext: { define: (name, definition) => { definitions[name] = definition; },
        override: (klass, override) => overrides.set(klass, override), htmlEncode: value => value },
};
context.window = context;
for (const language of ['en', 'pl']) {
    vm.runInNewContext(fs.readFileSync('frontend/locales/' + language + '.js', 'utf8'), context);
}
vm.runInNewContext(fs.readFileSync('frontend/betterdocker.js', 'utf8'), context);
const wizard = { callParent, down: () => null, query: () => [] };
definitions['PVE.betterdocker.CreateWizard'].initComponent.call(wizard);
assert(wizard.initialized);
assert.deepEqual(Array.from(wizard.items, item => item.title), ['General', 'Deployment', 'Network', 'Confirm']);
const fields = [...wizard.items[0].column1, ...wizard.items[0].column2, ...wizard.items[1].items];
assert.equal(fields.find(field => field.name === 'mode').value, 'compose');
assert.equal(fields.find(field => field.itemId === 'imageFields').hidden, true);
assert.equal(fields.find(field => field.itemId === 'imageFields').items[0].disabled, true);
assert(!JSON.stringify(wizard.items).includes('ostemplate'));
assert(!JSON.stringify(wizard.items).includes('pveLxc'));
const network = wizard.items[2];
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
assert.equal(inserted[0].text, 'Create DCT');
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
context.Ext.state = { Manager: { get: () => ({}) } };
const configPanel = { pveSelNode: { data: { id: 'docker/' + 'a'.repeat(32), node: 'pve', name: 'web' } },
    callParent, on() {}, down() { return null; } };
definitions['PVE.betterdocker.Config'].initComponent.call(configPanel);
assert.equal(definitions['PVE.betterdocker.Config'].extend, 'PVE.panel.Config');
assert.deepEqual(Array.from(configPanel.tbar.filter(item => item && item.text), item => item.text), ['Start', 'Shutdown', 'Migrate', 'Console', 'More']);
assert(!configPanel.tbar.find(item => item.text === 'More').menu.some(item => item.text === 'Migrate'));
assert.deepEqual(Array.from(configPanel.items, card => card.title),
    ['Summary', 'Console', 'Services', 'Resources', 'Network', 'DNS', 'Options', 'Task History', 'Snapshots', 'Backup', 'Replication', 'Deployment', 'Logs', 'Firewall', 'Options', 'Alias', 'IPSet', 'Log', 'Permissions']);
assert.equal(configPanel.items.find(card => card.title === 'Backup').xtype, 'pveBackupView');
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
context.Proxmox.Utils = { format_boolean: value => value ? 'Yes' : 'No', API2Request: options => { consoleRequest = options; } };
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
        { getValue: () => id === '#consoleContainer' ? 'b'.repeat(64) : '/bin/sh' }; },
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

const summaryEvents = {}, chartCalls = [], chartConfigs = [];
const nativeStore = { startUpdate() { chartCalls.push('start'); }, stopUpdate() { chartCalls.push('stop'); }, destroy() { chartCalls.push('destroy'); } };
context.Ext.create = (name, config) => { if (name === 'Proxmox.data.RRDStore') { chartConfigs.push(config); } return nativeStore; };
context.Ext.state = { Manager: { get: () => ({}), getProvider: () => ({}) } };
const summaryView = { apiUrl: '/nodes/pve/docker/' + 'a'.repeat(32), projectName: 'app',
    pveSelNode: configPanel.pveSelNode, callParent, on(event, handler) { summaryEvents[event] = handler; }, mon() {} };
definitions['PVE.betterdocker.Summary'].initComponent.call(summaryView);
assert(summaryView.tbar.some(item => item.xtype === 'proxmoxRRDTypeSelector'));
const summaryItems = summaryView.items[0].items;
assert.equal(summaryItems[0].layout.type, 'hbox');
assert.equal(summaryItems[0].items[1].xtype, 'pveBetterDockerNotes');
assert(summaryItems[0].items[0].items.some(item => item.xtype === 'pmxInfoWidget' && item.itemId === 'memoryValue'));
assert.equal(summaryItems.filter(item => item.xtype === 'proxmoxRRDChart').length, 4);
const resourceView = configPanel.items.find(card => card.itemId === 'resources');
assert.equal(resourceView.layout, 'fit');
assert.equal(resourceView.items.length, 1, 'mounts and limits share the native-style resource grid');
assert.equal(resourceView.items[0].xtype, 'pveBetterDockerPendingProperties');
assert.equal(definitions['PVE.betterdocker.PendingProperties'].hideHeaders, true);
assert.equal(definitions['PVE.betterdocker.PendingProperties'].extend, 'Proxmox.grid.PendingObjectGrid');
console.log('Native status widgets, Notes, RRD charts and full-size resource grid passed');
const summaryFields = {};
context.Proxmox.Utils.format_duration_long = value => String(value);
context.Proxmox.Utils.render_cpu_usage = (value, cpus) => `${value * 100}% of ${cpus} CPU(s)`;
context.Proxmox.Utils.render_size_usage = (used, max) => `${used} / ${max}`;
const renderedSummary = { down(id) { return id === '#statusPanel' ? { setTitle: value => { summaryFields.title = value; } } :
    { setText: text => { summaryFields[id] = { text }; }, updateValue: (text, usage) => { summaryFields[id] = { text, usage }; }, setPrintBar() {} }; } };
const summaryData = { name: 'app', node: 'pve', status: 'running', address: '192.0.2.32',
    containers: [{ state: 'running' }], services: [{ name: 'app', settings: { memory: 512, swap: 0, cpus: 1, cpuunits: 0,
        pids: 0, dns: [], dns_search: [], restart: 'always' }, mounts: [], ports: [] }],
    deployment: { mode: 'compose', compose: 'services: {}' }, metrics: { uptime: 60, cpu: 0.25, cpus: 1, mem: 128, maxmem: 512, swap: 32, maxswap: 128, disk: 100, maxdisk: 1000 } };
definitions['PVE.betterdocker.Summary'].updateSnapshot.call(renderedSummary, summaryData);
assert.equal(summaryFields['#memoryValue'].usage, 0.25);
assert.equal(summaryFields['#cpuValue'].usage, 0.25);
assert(summaryFields.title.includes('Uptime: 60'));
definitions['PVE.betterdocker.Summary'].updateSnapshot.call(renderedSummary, { ...summaryData, ha: { managed: 1, state: 'started' } });
assert.equal(summaryFields['#haValue'].text, 'started');
definitions['PVE.betterdocker.Summary'].updateSnapshot.call(renderedSummary, { ...summaryData, ha_recovery: { source: 'other' }, metrics: {} });
assert.equal(summaryFields['#cpuValue'].text, '');
definitions['PVE.betterdocker.Summary'].updateSnapshot.call(renderedSummary, summaryData);
// Only Summary exists initially: inactive native navigation cards are created lazily.
const panelEvents = {};
const lazyPanel = { pveSelNode: configPanel.pveSelNode, callParent,
    on(event, callback) { panelEvents[event] = callback; }, query() { return []; },
    down(id) { return id === '#summary' ? { updateSnapshot() {}, down() { return null; } } : null; } };
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
configPanel.query = () => [];
context.Ext.create = (name, options) => { createdWindows.push({ name, options }); return {}; };
configPanel.reloadDocker = () => {};
const shutdownButton = configPanel.tbar.find(button => button.itemId === 'stopButton');
shutdownButton.handler();
assert.equal(consoleRequest.params.action, 'shutdown');
consoleRequest.success({ result: { data: 'UPID:stop' } });
configPanel.pendingTask = null;
shutdownButton.menu[0].handler();
assert.equal(consoleRequest.params.action, 'reboot');
consoleRequest.success({ result: { data: 'UPID:restart' } });
configPanel.pendingTask = null;
assert.equal(createdWindows.length, 0, 'shutdown and restart must not open TaskViewer');
configPanel.tbar.find(button => button.itemId === 'startButton').handler();
consoleRequest.success({ result: { data: 'UPID:start' } });
configPanel.pendingTask = null;
assert.equal(createdWindows.length, 0, 'start must not open TaskViewer');
const clickedRecord = { data: { id: 'docker/' + 'c'.repeat(32), type: 'docker', node: 'pve-other', name: 'other-project', status: 'running' } };
const navigation = [];
const menuWorkspace = { selectById: id => navigation.push(id), query: () => [
    { pveSelNode: clickedRecord, selectById: card => navigation.push(card) }] };
const commandMenu = { pveSelNode: clickedRecord, workspace: menuWorkspace, callParent };
definitions['PVE.betterdocker.CmdMenu'].initComponent.call(commandMenu);
assert(commandMenu.items.find(item => item.text === 'Start').disabled);
assert.deepEqual(Array.from(commandMenu.items.filter(item => item.text).slice(0, 8), item => item.text), ['Start', 'Shutdown', 'Stop', 'Reboot', 'Clone', 'Migrate', 'Convert to template', 'Console']);
commandMenu.items.find(item => item.text === 'Reboot').handler();
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
    if (name === 'Proxmox.window.SafeDestroy') { return {}; }
    if (name === 'PVE.betterdocker.GuestStop') { return { show() {} }; }
    const menu = Object.assign({
        callParent,
        on(event, callback) { assert.equal(event, 'hide'); this.onHide = callback; },
        showAt(xy) { shownAt = xy; },
        down(id) { const item = this.items.find(entry => '#' + entry.itemId === id); return { setDisabled: value => { item.disabled = value; }, setVisible: value => { item.hidden = !value; } }; },
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
for (const label of ['Start', 'Shutdown', 'Reboot', 'Stop', 'Console']) {
    const record = { data: Object.assign({}, clickedRecord.data, { status: ['Start', 'Remove'].includes(label) ? 'stopped' : 'running' }) };
    const menu = context.PVE.Utils.createCmdMenu(treeView, record, null, 0, { stopEvent() {}, getXY() { return [100, 200]; } });
    const item = menu.items.find(entry => entry.text === label);
    if (label === 'Remove') { consoleRequest.success({ result: { data: { protection: 0, status: 'stopped', capabilities: { power: 1, configure: 1, console: 1 } } } }); }
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
    if (label === 'Remove') { assert.equal(menuArguments.name, 'Proxmox.window.SafeDestroy'); assert.equal(menuArguments.options.item.id, record.data.name); }
    else if (label === 'Stop') { assert.equal(menuArguments.name, 'PVE.betterdocker.GuestStop'); menuArguments.options.onConfirm(false); assert.equal(consoleRequest.params.action, 'force-stop'); }
    else if (label === 'Console') { assert.equal(navigation.at(-1), 'console'); }
    else if (label === 'Edit deployment') { assert.equal(consoleRequest.method, 'GET'); }
    else { assert.equal(consoleRequest.params.action, { Start: 'start', Shutdown: 'shutdown', Reboot: 'reboot', Stop: 'force-stop' }[label]); }
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
assert.equal(wizard.items[0].column1.find(item => item.name === 'vmid').emptyText, 'Next available guest ID');
console.log('Readable DCT task descriptions and numeric ID wizard passed');

for (const [handler, operation] of [
    [shutdownButton.handler, 'shutdown'], [shutdownButton.menu[0].handler, 'reboot'],
    [commandMenu.items.find(item => item.text === 'Shutdown').handler, 'shutdown'],
    [commandMenu.items.find(item => item.text === 'Reboot').handler, 'reboot'],
]) {
    let answer;
    const previousRequest = consoleRequest;
    context.Ext.Msg.confirm = (title, message, callback) => {
        assert.equal(title, 'Confirm');
        assert(message.includes({ shutdown: 'Shutdown', reboot: 'Reboot', 'force-stop': 'Stop' }[operation]));
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
    configPanel.pendingTask = null;
    assert.equal(createdWindows.length, windowsBefore, 'Confirmed action must not open task output');
}
context.Ext.Msg.confirm = () => { throw new Error('Start must not ask for confirmation'); };
configPanel.tbar.find(button => button.itemId === 'startButton').handler();
assert.equal(consoleRequest.params.action, 'start');
console.log('Shutdown/restart confirmation and cancellation in toolbar and context menu passed');

// Authentication is applied after workspace initialization on a fresh sign-in.
let createDisabled = true;
const signedIn = { down: () => ({ setDisabled(value) { createDisabled = value; } }) };
context.Proxmox.UserName = 'root@pam';
overrides.get(workspaceClass).onLogin.call(signedIn, { cap: {} });
assert.equal(createDisabled, false);
overrides.get(workspaceClass).onLogin.call(signedIn, null);
assert.equal(createDisabled, true);
console.log('Create DCT updates after sign-in and sign-out passed');

// A running task blocks conflicting requests until the native task endpoint completes.
let taskTick;
context.setInterval = fn => { taskTick = fn; return 2; };
context.Ext.Msg.confirm = (_title, _message, callback) => callback('yes');
configPanel.pendingTask = null;
configPanel.tbar.find(button => button.itemId === 'startButton').handler();
consoleRequest.success({ result: { data: 'UPID:pending' } });
const pendingRequest = consoleRequest;
shutdownButton.handler();
assert.equal(consoleRequest, pendingRequest);
taskTick();
assert(consoleRequest.url.includes('/tasks/UPID%3Apending/status'));
let taskErrorMask;
const taskAlerts = [];
context.Ext.Msg.alert = (...args) => taskAlerts.push(args);
context.Proxmox.Utils.setErrorMask = (_target, message) => { taskErrorMask = message; };
consoleRequest.failure({ htmlStatus: 'Connection failed' });
assert.equal(taskErrorMask, undefined, 'task polling must not mask the guest view');
assert.equal(taskAlerts.length, 0, 'a transient missed poll must not open a modal');
assert.equal(configPanel.pendingTask, 'UPID:pending', 'a connection error does not mean the task has finished');
taskTick();
consoleRequest.success({ result: { data: { status: 'stopped', exitstatus: 'OK' } } });
assert.equal(taskErrorMask, undefined, 'task polling must not clear unrelated masks');
assert.equal(configPanel.pendingTask, null);
// Completion of the interrupted Shutdown must not unlock the newer Stop task.
configPanel.tbar.find(button => button.itemId === 'startButton').handler();
consoleRequest.success({ result: { data: 'UPID:old-shutdown' } });
taskTick();
const oldTaskPoll = consoleRequest;
configPanel.pendingTask = 'UPID:new-stop';
oldTaskPoll.success({ result: { data: { status: 'stopped', exitstatus: 'Shutdown interrupted by Stop.' } } });
assert.equal(configPanel.pendingTask, 'UPID:new-stop');
configPanel.pendingTask = null;
// Quiet tasks report errors/warnings once; an existing task dialog owns its feedback.
for (const [status, visible, expectedTitle] of [['OK', false, null], ['WARNINGS: 1', false, 'Warning'], ['failed', false, 'Error'], ['failed', true, null]]) {
    configPanel.tbar.find(button => button.itemId === 'startButton').handler();
    const response = { result: { data: 'UPID:pve:123:456:789:test:100:root@pam:' }, dockerTaskVisible: visible };
    consoleRequest.success(response);
    const before = taskAlerts.length;
    taskTick();
    const completion = consoleRequest;
    completion.success({ result: { data: { status: 'stopped', exitstatus: status } } });
    completion.success({ result: { data: { status: 'stopped', exitstatus: status } } });
    if (expectedTitle === 'Error') {
        assert(consoleRequest.url.endsWith('/log'));
        consoleRequest.success({ result: { data: [{ t: 'BetterDocker: meaningful failure' }] } });
        assert.equal(taskAlerts.at(-1)[1], 'meaningful failure');
    }
    assert.equal(taskAlerts.length, before + (expectedTitle ? 1 : 0));
    if (expectedTitle) assert.equal(taskAlerts.at(-1)[0], expectedTitle);
    assert.equal(configPanel.pendingTask, null);
}
console.log('Task completion refresh and conflicting operation exclusion passed');

assert.equal(configPanel.items.find(card => card.itemId === 'tasks').xtype, 'proxmoxNodeTasks');
assert.equal(configPanel.items.find(card => card.itemId === 'tasks').preFilter.vmid, configPanel.pveSelNode.data.vmid);
assert.equal(configPanel.tbar.filter(button => button.text === 'Help').length, 0, 'native Config supplies Help');
assert.equal(definitions['PVE.betterdocker.DeploymentEdit'].extend, 'Proxmox.window.Edit');
assert.equal(definitions['PVE.betterdocker.CodeArea'].extend, 'Ext.form.field.TextArea');
assert(fs.readFileSync('frontend/betterdocker.css', 'utf8').includes('background-color: currentColor'));
console.log('Native task filtering, single Help, native editor and theme-aware icons passed');

// Native view filters must retain DCTs without broadening other resource types.
for (const id of ['pool', 'tags', 'server']) {
    for (const property of ['filterfn', 'getFilterFn']) {
        const nativeFilter = record => ['lxc', 'qemu', 'pool'].includes(record.data.type);
        const selector = { getViewFilter() {
            return { id, [property]: property === 'filterfn' ? nativeFilter : () => nativeFilter };
        } };
        overrides.get(viewSelectorClass).initComponent.call(selector);
        assert(selector.nativeInitialized);
        const view = selector.getViewFilter();
        const filter = property === 'filterfn' ? view.filterfn : view.getFilterFn();
        for (const type of ['lxc', 'qemu', 'pool']) { assert(filter({ data: { type } })); }
        assert.equal(filter({ data: { type: 'docker' } }), id !== 'server');
        assert.equal(filter({ data: { type: 'storage' } }), false);
    }
}
console.log('Pool and Tag View filters include DCTs and preserve native filtering');

// Native Tag View refresh must resolve all tagged clones to the original record.
const sourceRecord = { data: { id: 'docker/' + 'a'.repeat(32), tags: 'prod;ui-smoke' } };
const unrelatedRecord = { data: { id: 'lxc/100' } };
const collection = { get(key) { return key === sourceRecord.data.id ? sourceRecord : key === 'lxc/100' ? unrelatedRecord : undefined; } };
context.PVE.data = { ResourceStore: { data: collection } };
overrides.get(workspaceClass).initComponent.call({ down() { return null; } });
assert.equal(collection.get(sourceRecord.data.id + '-ui-smoke'), sourceRecord);
assert.equal(collection.get(sourceRecord.data.id + '-prod'), sourceRecord);
assert.equal(collection.get('lxc/100'), unrelatedRecord);
assert.equal(collection.get('docker/' + 'b'.repeat(32) + '-prod'), undefined);
const wrappedLookup = collection.get;
overrides.get(workspaceClass).initComponent.call({ down() { return null; } });
assert.equal(collection.get, wrappedLookup, 'install once when workspaces are recreated');
console.log('Tag View refresh retains DCT clones and leaves unrelated lookups unchanged');

const tagClone = { data: { id: sourceRecord.data.id + '-ui-smoke', realId: sourceRecord.data.id } };
const taggedWorkspace = { down() { return { getStore() { return { getRootNode() { return { findChildBy(predicate) { return predicate(tagClone) ? tagClone : undefined; } }; } }; } }; } };
assert.equal(overrides.get(workspaceClass).selectById.call(taggedWorkspace, sourceRecord.data.id), tagClone.data.id);
assert.equal(overrides.get(workspaceClass).selectById.call(taggedWorkspace, 'lxc/100'), 'lxc/100');
console.log('Global resource navigation resolves tagged DCT IDs without changing VM/CT routes');

const diagnosticsMenu = { pveSelNode: { data: { node: "pve" } }, insertNodes(items) { this.menuItems = items; }, add() { throw new Error("Config cards must register native navigation"); } };
overrides.get(nodeConfigClass).initComponent.call(diagnosticsMenu);
assert(diagnosticsMenu.nativeInitialized);
assert.equal(diagnosticsMenu.menuItems[0].itemId, "betterdocker");
assert.equal(diagnosticsMenu.menuItems[0].xtype, "pveBetterDockerDiagnostics");
console.log("Node diagnostics registers a native navigation item and lazy card");

context.Proxmox.Utils.override_task_descriptions = descriptions => { context.taskDescriptions = descriptions; };
context.gettext = value => ({ Summary: 'Podsumowanie', Network: 'Sieć' })[value] || value;
vm.runInNewContext(fs.readFileSync('frontend/betterdocker.js', 'utf8'), context);
const polishWizard = { callParent, down: () => null, query: () => [] };
definitions['PVE.betterdocker.CreateWizard'].initComponent.call(polishWizard);
assert.deepEqual(Array.from(polishWizard.items, item => item.title), ['Główne', 'Wdrożenie', 'Sieć', 'Potwierdź']);
const polishPanel = { pveSelNode: { data: { id: 'docker/' + 'a'.repeat(32), node: 'pve', name: 'web', vmid: 103 } }, callParent, on() {}, down() { return null; } };
definitions['PVE.betterdocker.Config'].initComponent.call(polishPanel);
assert.equal(polishPanel.title, "DCT 103 (web) na węźle 'pve'");
assert.equal(context.taskDescriptions.betterdockersettings('betterdockersettings', '103'), 'DCT 103 - Edytuj zasoby / opcje');
console.log('Polish wizard, node heading and settings task description passed');

assert(!fs.readFileSync("frontend/betterdocker.js", "utf8").includes("background-image"));
assert(!fs.readFileSync("frontend/betterdocker.css", "utf8").includes("bd-code"));
console.log("Compose fields have no decorative line-number background");

assert.equal(definitions['PVE.betterdocker.CreateWizard'].width, 760);
assert.equal(definitions['PVE.betterdocker.CreateWizard'].height, 570);
assert.equal(definitions['PVE.betterdocker.DeploymentEdit'].width, 600);
assert(wizard.items[0].column1 && wizard.items[0].column2);
assert.equal(wizard.items[1].advancedItems[0].name, 'environment');
assert(!JSON.stringify(wizard.items).includes('sourceLines'));
console.log('Native LXC wizard dimensions, columns and advanced environment passed');

// The native Snapshot window's value hook is not used by every PVE toolkit version.
// DCT submission must send its action explicitly and keep saved snapshots immutable.
let snapshotRequest;
context.Proxmox.Utils.API2Request = options => { snapshotRequest = options; };
const snapshotWindow = { isCreate: true, url: '/nodes/pve/docker/project/snapshot',
    formPanel: { getForm: () => ({ isValid: () => true, getValues: () => ({ snapname: 'after', description: 'second', vmstate: 1 }) }) } };
definitions['PVE.betterdocker.SnapshotEdit'].submit.call(snapshotWindow);
assert.equal(snapshotRequest.method, 'POST');
assert.deepEqual(JSON.parse(JSON.stringify(snapshotRequest.params)), { action: 'create', snapname: 'after', description: 'second' });
snapshotWindow.isCreate = false;
snapshotWindow.url += '/after/config';
definitions['PVE.betterdocker.SnapshotEdit'].submit.call(snapshotWindow);
assert.equal(snapshotRequest.method, 'PUT');
assert.deepEqual(JSON.parse(JSON.stringify(snapshotRequest.params)), { description: 'second' });
snapshotWindow.viewonly = true;
snapshotRequest = null;
definitions['PVE.betterdocker.SnapshotEdit'].submit.call(snapshotWindow);
assert.equal(snapshotRequest, null);
console.log('Native snapshot submission, immutable names and view-only access passed');

const locales = context.PVE.betterdockerLocales;
assert.deepEqual(Object.keys(locales.en.messages).sort(), Object.keys(locales.pl.messages).sort());
for (const key of Object.keys(locales.en.messages)) {
    assert(fs.readFileSync('frontend/betterdocker.js', 'utf8').includes(key), `Unused translation: ${key}`);
}
context.gettext = value => ({ Summary: 'Podsumowanie', Deployment: 'Native deployment' })[value] || value;
const nativeTranslationWizard = { callParent, down: () => null, query: () => [] };
definitions['PVE.betterdocker.CreateWizard'].initComponent.call(nativeTranslationWizard);
assert.equal(nativeTranslationWizard.items[1].title, 'Native deployment');
const savedPolishDeployment = locales.pl.messages.Deployment;
delete locales.pl.messages.Deployment;
context.gettext = value => value === 'Summary' ? 'Podsumowanie' : value;
const fallbackWizard = { callParent, down: () => null, query: () => [] };
definitions['PVE.betterdocker.CreateWizard'].initComponent.call(fallbackWizard);
assert.equal(fallbackWizard.items[1].title, locales.en.messages.Deployment);
locales.pl.messages.Deployment = savedPolishDeployment;
context.gettext = value => value === 'Summary' ? 'Zusammenfassung' : value;
const otherLanguageWizard = { callParent, down: () => null, query: () => [] };
definitions['PVE.betterdocker.CreateWizard'].initComponent.call(otherLanguageWizard);
assert.deepEqual(Array.from(otherLanguageWizard.items, item => item.title), ['General', 'Deployment', 'Network', 'Confirm']);
console.log('Separate language catalogs, native translation priority and English fallback passed');

// Missing capacities must not turn measured usage into zero or a false percentage.
context.Proxmox.Utils.format_size = value => `${value} B`;
const summaryBars = {};
const unlimitedSummary = { down(id) { return id === '#statusPanel' ? { setTitle() {} } : {
    updateValue(text) { summaryFields[id] = { text }; },
    setPrintBar(value) { summaryBars[id] = value; },
}; } };
definitions['PVE.betterdocker.Summary'].updateSnapshot.call(unlimitedSummary, {
    ...summaryData, metrics: { ...summaryData.metrics, maxmem: 0, maxswap: 0, maxdisk: 0 },
});
assert.equal(summaryFields['#memoryValue'].text, '128 B');
assert.equal(summaryFields['#swapValue'].text, '32 B');
assert.equal(summaryFields['#diskValue'].text, '100 B');
assert.equal(summaryBars['#memoryValue'], false);
assert.equal(summaryBars['#swapValue'], false);
assert.equal(summaryBars['#diskValue'], false);

// Log instance choices stay within the selected service across refreshes.
const logRequests = [], logEvents = {}, logControls = {};
context.Proxmox.Utils.API2Request = options => { logRequests.push(options); };
const logPanel = { apiUrl: '/nodes/pve/docker/project', callParent,
    on(event, handler) { logEvents[event] = handler; }, down(id) { return logControls[id]; },
};
definitions['PVE.betterdocker.Logs'].initComponent.call(logPanel);
for (const spec of [...logPanel.tbar, ...logPanel.bbar, ...logPanel.items]) {
    if (!spec.itemId) { continue; }
    const store = { loadData(rows) { this.rows = rows; } };
    logControls['#' + spec.itemId] = { value: spec.value ?? spec.checked ?? '',
        getValue() { return this.value; }, setValue(value) { this.value = value; }, getStore() { return store; },
    };
}
logEvents.afterrender();
const logTargets = { services: [{ name: 'web' }, { name: 'db' }], containers: [
    { id: 'web-1', service: 'web', name: 'web-1' }, { id: 'db-1', service: 'db', name: 'db-1' },
] };
logRequests.find(request => request.url.endsWith('/management')).success({ result: { data: logTargets } });
const instancePicker = logControls['#logContainer'];
instancePicker.setValue('db-1');
logControls['#logService'].setValue('web');
logPanel.tbar.find(item => item.itemId === 'logService').listeners.select();
assert.deepEqual(Array.from(instancePicker.getStore().rows, row => row.id), ['', 'web-1']);
assert.equal(instancePicker.getValue(), '');
instancePicker.setValue('web-1');
logEvents.activate();
logRequests.filter(request => request.url.endsWith('/management')).at(-1).success({ result: { data: logTargets } });
assert.equal(instancePicker.getValue(), 'web-1');
logRequests.filter(request => request.url.endsWith('/management')).at(-1).success({ result: { data: { services: logTargets.services, containers: [] } } });
assert.equal(instancePicker.getValue(), '');

// An image deployment exports text; Compose exports YAML.
const deploymentExports = [];
context.Blob = class {};
context.URL = { createObjectURL: () => 'blob:test', revokeObjectURL() {} };
context.document = { createElement: () => ({ click() { deploymentExports.push(this.download); } }) };
lazyPanel.down = id => id === '#source' ? { getValue: () => 'Image: nginx' } : null;
const exportButton = lazyPanel.items.find(item => item.itemId === 'deployment').tbar.find(item => item.text === 'Download');
summaryData.deployment = { mode: 'image', image: 'nginx' };
exportButton.handler();
summaryData.deployment = { mode: 'compose', compose: 'services: {}' };
exportButton.handler();
assert.deepEqual(deploymentExports, ['deployment.txt', 'compose.yaml']);
console.log('Measured usage without capacities, service-scoped log filters and deployment exports passed');

// File read failures must preserve the editor and allow selecting the file again.
(async () => {
    const alerts = [];
    context.Ext.Msg.alert = (title, message) => alerts.push({ title, message });
    const importButton = wizard.items[1].items[0].items[1].items[0];
    assert.equal(importButton.xclass, 'Ext.form.field.FileButton', 'imports use a button without a text-field layout');
    const importHandler = importButton.listeners.change;
    let resets = 0, writes = 0;
    const upload = { fileInputEl: { dom: { files: [{ size: 10, text: async () => { throw new Error('read failed'); } }] } },
        reset(clear) { assert.equal(clear, true); resets++; }, up: () => ({ down: () => ({ setValue() { writes++; } }) }),
    };
    await importHandler.call(upload);
    assert.equal(resets, 1);
    assert.equal(writes, 0);
    assert.equal(alerts[0].message, 'Could not read file');
    upload.fileInputEl.dom.files[0].text = async () => 'services: {}';
    await importHandler.call(upload);
    assert.equal(resets, 2);
    assert.equal(writes, 1);
    console.log('Failed file import preserves content and permits retry');
})().catch(error => { console.error(error); process.exitCode = 1; });

// Cached month/year selections must never request unavailable history.
context.Proxmox.data = { RRDStore: { prototype: { setRRDUrl(timeframe, cf) {
    this.requestRange = timeframe; this.requestCF = cf;
} } } };
const chartState = { timeframe: 'year' };
chartConfigs[0].setRRDUrl.call(chartState, undefined, 'MAX');
assert.equal(chartState.requestRange, 'hour');
assert.equal(chartState.requestCF, 'MAX');
chartConfigs[0].setRRDUrl.call(chartState, 'week', 'AVERAGE');
assert.equal(chartState.requestRange, 'week');
let chartSelection = 'year', allowedRanges;
summaryView.tbar.find(item => item.xtype === 'proxmoxRRDTypeSelector').listeners.afterrender({
    lookup: () => ({ getValue: () => chartSelection, setValue: value => { chartSelection = value; },
        getStore: () => ({ filterBy: predicate => {
            allowedRanges = ['hour', 'day', 'week', 'month', 'year'].filter(id => predicate({ get: () => id }));
        } }),
    }),
});
assert.deepEqual(allowedRanges, ['hour', 'day', 'week']);
assert.equal(chartSelection, 'hour');
summaryEvents.deactivate();
assert.equal(chartCalls.at(-1), 'stop');
summaryEvents.activate();
assert.equal(chartCalls.at(-1), 'start');
summaryEvents.hide();
assert.equal(chartCalls.at(-1), 'stop');
summaryEvents.destroy();
assert.equal(chartCalls.at(-1), 'stop');
assert(!chartCalls.includes('destroy'), 'destroying an active native RRD store breaks its in-flight callbacks');

// A slow or disconnected backend must not accumulate polling requests.
const statusEvents = {}, statusRequests = [];
let statusTick, refreshInterval, statusVisible = true;
context.document.hidden = false;
context.setInterval = (handler, milliseconds) => { statusTick = handler; refreshInterval = milliseconds; return 99; };
context.Proxmox.Utils.API2Request = options => statusRequests.push(options);
const statusPanel = { pveSelNode: configPanel.pveSelNode, callParent, query: () => [], down: () => null,
    on(event, handler) { statusEvents[event] = handler; }, isVisible: () => statusVisible,
};
definitions['PVE.betterdocker.Config'].initComponent.call(statusPanel);
statusEvents.afterrender();
assert.equal(refreshInterval, 1000);
statusTick(); statusTick();
assert.equal(statusRequests.length, 1, 'an outstanding request blocks additional polls');
statusRequests[0].failure({ htmlStatus: 'Connection failed' });
statusVisible = false;
statusTick();
assert.equal(statusRequests.length, 1);
statusVisible = true;
context.document.hidden = true;
statusTick();
assert.equal(statusRequests.length, 1);
context.document.hidden = false;
statusTick();
assert.equal(statusRequests.length, 2, 'polling resumes after a connection failure');
statusRequests[1].success({ result: { data: { ...summaryData, status: 'stopped', containers: [] } } });
statusTick();
assert.equal(statusRequests.length, 3);
statusRequests[2].success({ result: { data: { ...summaryData, status: 'partial' } } });
statusPanel.destroyed = true;
statusTick();
assert.equal(statusRequests.length, 3);
console.log('Supported chart history, hidden chart lifecycle and non-overlapping status recovery passed');

const initialWidgets = summaryItems[0].items[0].items.filter(item => item.xtype === 'pmxInfoWidget');
assert(initialWidgets.every(item => item.text === ''), 'initial measurements remain empty');
for (const [status, expected] of [['running', 'Running'], ['stopped', 'Stopped'], ['partial', 'Partially running']]) {
    definitions['PVE.betterdocker.Summary'].updateSnapshot.call(unlimitedSummary, { ...summaryData, status });
    assert.equal(summaryFields['#statusValue'].text, expected);
}
definitions['PVE.betterdocker.Summary'].updateSnapshot.call(unlimitedSummary, {
    ...summaryData, metrics: { ...summaryData.metrics, disk: null, maxdisk: null, swap: null, maxswap: null },
});
assert.equal(summaryFields['#diskValue'].text, 'Unavailable');
assert.equal(summaryFields['#swapValue'].text, 'Unavailable');
assert.equal(summaryBars['#diskValue'], false);
console.log('Empty initial values, running/stopped/partial status and unavailable storage passed');

// ExtJS chart configs apply explicitly present undefined values instead of defaults.
// applyColors expects an array and crashes if an override passes undefined.
for (const chart of summaryItems.filter(item => item.xtype === 'proxmoxRRDChart')) {
    if (Object.hasOwn(chart, 'colors')) {
        assert(Array.isArray(chart.colors));
        assert.doesNotThrow(() => Array.from(chart.colors, color => color));
    }
    assert(Object.values(chart).every(value => value !== undefined), 'chart defaults must be omitted, not undefined');
}
console.log('ExtJS chart defaults are preserved and explicit color palettes are valid arrays');

// Both toolbar actions preserve the project identity and use native removal gating.
configPanel.pveSelNode.data.vmid = 101;
const stage2Windows = [];
context.Ext.create = (name, options) => { stage2Windows.push({ name, options }); return {}; };
configPanel.pendingTask = null;
const requestsBeforeRemove = statusRequests.length;
configPanel.tbar.find(item => item.text === 'More').menu.find(item => item.text === 'Remove').handler();
const removal = stage2Windows.at(-1);
assert.equal(removal.name, 'Proxmox.window.SafeDestroy');
assert.equal(removal.options.item.id, configPanel.pveSelNode.data.vmid);
assert(removal.options.note.includes('retained'));
assert.equal(removal.options.showProgress, true);
assert.equal(statusRequests.length, requestsBeforeRemove, 'opening the dialog cannot remove a project');
configPanel.tbar.find(item => item.itemId === 'consoleButton').menu[0].handler();
const detachedConsole = stage2Windows.at(-1);
assert.equal(detachedConsole.name, 'Ext.window.Window');
assert.equal(detachedConsole.options.items[0].xtype, 'pveBetterDockerConsole');
assert.equal(detachedConsole.options.items[0].apiUrl, removal.options.url);
assert.equal(shutdownButton.menu[1].text, 'Stop');
assert.equal(shutdownButton.menu[0].text, 'Reboot');
console.log('Native removal dialog and separate console keep the selected DCT scope');

// Stop uses the native MessageBox/checkbox pattern and sends override only after confirmation.
const stopWindows = [];
context.Ext.create = (name, options) => { stopWindows.push({ name, options }); return { show() {} }; };
configPanel.pendingTask = null;
const stopRequestBefore = statusRequests.length;
shutdownButton.menu[1].handler();
assert.equal(statusRequests.length, stopRequestBefore);
const stopDialog = stopWindows.at(-1);
assert.equal(stopDialog.name, 'PVE.betterdocker.GuestStop');
assert.equal(stopDialog.options.activeShutdown, false);
stopDialog.options.onConfirm(true);
assert.equal(statusRequests.at(-1).params['overrule-shutdown'], 1);
assert.equal(statusRequests.at(-1).params.action, 'force-stop');
const guestStopClass = definitions['PVE.betterdocker.GuestStop'];
let fieldConfig, confirmationConfig, destroyed = false, performed = false;
const fakeStop = { activeShutdown: true, canOverrule: true, message: 'Stop DCT 105?',
    onConfirm(override) { performed = override; }, destroy() { destroyed = true; },
    callParent(args) { if (args) confirmationConfig = args[0]; },
    promptContainer: { add(config) { fieldConfig = config; }, down() { return { isDisabled: () => false, getSubmitValue: () => true }; } } };
guestStopClass.initComponent.call(fakeStop);
assert.equal(fieldConfig.name, 'overrule-shutdown');
assert.equal(fieldConfig.checked, true);
guestStopClass.show.call(fakeStop);
confirmationConfig.callback('no');
assert.equal(performed, false);
assert.equal(destroyed, true);
destroyed = false;
confirmationConfig.callback('yes');
assert.equal(performed, true);
assert.equal(destroyed, true);
console.log('Stop override requires confirmation and cancellation submits no request');
// Active Shutdown keeps Stop reachable, including its splitbutton parent.
const overrideEvents = {}, disabledPower = {};
const overridePanel = { pveSelNode: configPanel.pveSelNode, callParent, pendingTask: 'UPID:shutdown',
    on(event, callback) { overrideEvents[event] = callback; }, query() { return []; },
    down(id) {
        if (id === '#summary') return { updateSnapshot() {}, down() { return null; } };
        if (['#startButton', '#stopButton', '#restartButton', '#forceStopButton', '#consoleButton', '#removeButton'].includes(id)) {
            return { setDisabled(value) { disabledPower[id] = Boolean(value); } };
        }
        return null;
    } };
definitions['PVE.betterdocker.Config'].initComponent.call(overridePanel);
overrideEvents.afterrender();
statusRequests.at(-1).success({ result: { data: Object.assign({}, summaryData, {
    operation: { active: true, action: 'shutdown', service: null, user: 'root@pam' },
    capabilities: { power: 1, configure: 1, overrule_shutdown: 1 },
}) } });
assert.equal(disabledPower['#stopButton'], false, 'parent menu must remain reachable');
assert.equal(disabledPower['#forceStopButton'], false);
assert.equal(disabledPower['#restartButton'], true);
overridePanel.tbar.find(button => button.itemId === 'stopButton').menu[1].handler();
assert.equal(stopWindows.at(-1).options.activeShutdown, true);
assert.equal(stopWindows.at(-1).options.canOverrule, true);
stopWindows.at(-1).options.onConfirm(true);
assert.equal(statusRequests.at(-1).params['overrule-shutdown'], 1);
overrideEvents.beforedestroy();
console.log('Only Stop stays enabled during an authorized active Shutdown');
// Deployment editor keeps the digest it loaded, rather than a later polling result.
const digestRequests = [];
context.Proxmox.Utils.API2Request = options => digestRequests.push(options);
const editorDigest = '1'.repeat(64);
const digestEditor = { digest: editorDigest, vmid: 105, projectId: 'e'.repeat(32), nodename: 'pve',
    deployment: { name: 'digest-test', mode: 'compose', compose: 'services: {}', bridge: 'vmbr0', address: '192.0.2.250/20', ipv4mode: 'static' },
    callParent, down() { return null; }, formPanel: { getForm() { return { isValid: () => true }; } },
    getValues() { return { mode: 'compose', compose: 'services: {app: {image: busybox}}' }; } };
definitions['PVE.betterdocker.DeploymentEdit'].initComponent.call(digestEditor);
digestEditor.digest = '2'.repeat(64);
digestEditor.submit();
assert.equal(digestRequests.at(-1).method, 'PUT');
assert(digestRequests.at(-1).url.endsWith('/docker'));
digestRequests.at(-1).success({ result: { data: [] } });
assert.equal(digestRequests.at(-1).params.digest, editorDigest);
assert(digestRequests.at(-1).url.endsWith('/' + 'e'.repeat(32)));
console.log('Deployment editing submits the digest captured when the editor opened');
// Resource/DNS/options and disk editors retain their own version across status refreshes.
const digestEvents = {}, capturedEditors = [];
const digestPanel = { pveSelNode: configPanel.pveSelNode, callParent,
    on(event, callback) { digestEvents[event] = callback; }, query() { return []; },
    down(id) { return id === '#summary' ? { updateSnapshot() {}, down() { return null; } } : null; } };
context.Ext.create = (name, options) => {
    assert.equal(name, 'Proxmox.window.Edit');
    const editor = Object.assign({ formPanel: { getForm() { return { isValid: () => true,
        getValues: () => options.url.endsWith('/storage-limit') ? { size_mib: 256 } : options.title.includes('DNS') ?
            { dns: '1.1.1.1', dns_search: 'example.test' } : options.title.includes('Options') ? { restart: 'always' } : { memory: 128, swap: 0 } }; } },
        show() {}, close() {} }, options);
    capturedEditors.push(editor); return editor;
};
definitions['PVE.betterdocker.Config'].initComponent.call(digestPanel);
function loadDigest(digest, capabilities = { configure: 1, power: 1 }, extra = {}) {
    digestPanel.reloadDocker();
    digestRequests.at(-1).success({ result: { data: Object.assign({}, summaryData, {
        digest, operation: {}, capabilities, storage: { rootfs: {}, limit_mib: 0 },
        services: [{ name: 'app', image: 'busybox', ports: [], mounts: [], overridden: true,
            settings: { cpus: 1, memory: 32, swap: 0, cpuunits: 0, pids: 0, dns: [], dns_search: [], restart: 'no' } }],
    }, extra) } });
}
for (const [group, key] of [['resources', 'Memory'], ['dns', 'DNS servers'], ['options', 'Restart policy'], ['resources', 'Project data disk']]) {
    loadDigest(editorDigest, undefined, key === 'Project data disk' ? { containers: [] } : {});
    const grid = digestPanel.items.find(item => item.itemId === group).items[0];
    grid.listeners.itemdblclick(null, { get: () => key });
    const editor = capturedEditors.at(-1);
    assert(editor);
    loadDigest('2'.repeat(64));
    editor.submit.call(editor);
    assert.equal(digestRequests.at(-1).params.digest, editorDigest, group + ' must not use refreshed version');
    if (key !== 'Project data disk') {
        const before = capturedEditors.length;
        digestRequests.at(-1).success({ result: { data: 'UPID:pve:1:2:3:settings:100:root@pam:' } });
        assert.equal(capturedEditors.length, before, 'ordinary save must not open a task viewer');
        digestPanel.pendingTask = null;
    }
}
// Polling must not replace an unchanged row between the two mouse clicks.
const previousEditorFactory = context.Ext.create, previousDigestDown = digestPanel.down;
let networkLoads = 0, selectedNetwork;
const networkRows = [];
const networkSelection = { getSelection: () => selectedNetwork ? [selectedNetwork] : [],
    select(record) { selectedNetwork = record; } };
const networkStore = { loadData(data) { networkLoads++; networkRows.splice(0, networkRows.length,
    ...data.map(values => ({ get: key => values[key] }))); }, getRange: () => networkRows };
const networkGridMock = { getSelectionModel: () => networkSelection, getStore: () => networkStore };
digestPanel.down = id => id === '#networkGrid' ? networkGridMock : null;
loadDigest(editorDigest);
selectedNetwork = networkRows[0];
const originalNetworkRow = selectedNetwork;
loadDigest(editorDigest);
assert.equal(networkLoads, 1, 'unchanged polling must preserve the network row DOM');
assert.equal(selectedNetwork, originalNetworkRow);
const networkEditors = [];
context.Ext.create = (name, options) => {
    networkEditors.push({ name, options }); return { on() {}, show() {} };
};
const networkCard = digestPanel.items.find(item => item.itemId === 'network');
networkCard.items[0].listeners.itemdblclick();
assert.equal(networkEditors.at(-1).name, 'PVE.betterdocker.NetworkEdit');
loadDigest(editorDigest, { network: 0, configure: 0 });
networkCard.items[0].listeners.itemdblclick();
assert.equal(networkEditors.length, 1, 'double click must respect network permissions');
context.Ext.create = previousEditorFactory;
digestPanel.down = previousDigestDown;
loadDigest(editorDigest);
console.log('Network double click and stable polling passed');
digestEvents.beforedestroy();
console.log('Resource, DNS, options and disk edits preserve the version from opening');

const pendingProperties = definitions['PVE.betterdocker.PendingProperties'];
const pendingLoads = [], pendingEvents = [];
const pendingGrid = { rows: {}, rstore: { loadData(data) { pendingLoads.push(data); },
    getRange() { return pendingLoads.at(-1); }, fireEvent(...args) { pendingEvents.push(args); } } };
pendingProperties.setRows.call(pendingGrid, [{ key: 'CPU limit', value: 1, pending: 'Unlimited' },
    { key: 'DNS servers', value: 'Docker default', pending: '1.1.1.1' }]);
assert.equal(pendingLoads[0][0].pending, 'Unlimited');
assert.equal(pendingEvents[0][0], 'load');
assert.equal(pendingEvents[0][3], true, 'native DiffStore receives a successful load');
assert.equal(pendingGrid.rows['DNS servers'].renderer('1.1.1.1'), '1.1.1.1');
for (const [group, id] of [['resources', 'resourceGrid'], ['dns', 'dnsGrid'], ['options', 'optionGrid']]) {
    const card = digestPanel.items.find(item => item.itemId === group);
    assert.equal(card.items[0].xtype, 'pveBetterDockerPendingProperties');
    assert.equal(card.tbar.find(button => button.itemId === id + 'Revert').xtype, 'pvePendingRevertButton');
}
console.log('Native pending model, DiffStore updates and Revert controls on all settings pages passed');

let protectionRequest;
context.Proxmox.Utils.API2Request = options => { protectionRequest = options; };
const delegatedMenu = { pveSelNode: clickedRecord, workspace: menuWorkspace, callParent() {},
    down(id) { const item = this.items.find(entry => '#' + entry.itemId === id); return { setDisabled: value => { item.disabled = value; }, setVisible: value => { item.hidden = !value; } }; } };
context.Proxmox.UserName = 'delegate@pve';
definitions['PVE.betterdocker.CmdMenu'].initComponent.call(delegatedMenu);
protectionRequest.success({ result: { data: { status: 'running', protection: 0, operation: {},
    capabilities: { power: 1, configure: 0, console: 0 } } } });
assert.equal(delegatedMenu.items.find(item => item.itemId === 'shutdown').disabled, false);
assert.equal(delegatedMenu.items.find(item => item.itemId === 'console').disabled, true);
assert(!delegatedMenu.items.some(item => item.itemId === 'deployment'));
assert(!delegatedMenu.items.some(item => item.itemId === 'removeButton'));
assert.equal(delegatedMenu.items.find(item => item.itemId === 'clone').hidden, true);
context.Proxmox.UserName = 'root@pam';
console.log('Context menu uses per-project power grants without exposing host actions');

context.Proxmox.Utils.API2Request = options => digestRequests.push(options);
context.Proxmox.UserName = 'delegate@pve';
loadDigest(editorDigest, { configure: 0, cpu: 1, memory: 0, network: 0, options: 0, power: 1 });
const resourceRows = digestPanel.items.find(item => item.itemId === 'resources').items[0];
const beforeDeniedEdit = capturedEditors.length;
resourceRows.listeners.itemdblclick(null, { get: () => 'Memory' });
assert.equal(capturedEditors.length, beforeDeniedEdit, 'CPU privilege must not open a memory editor');
resourceRows.listeners.itemdblclick(null, { get: () => 'CPU limit' });
assert.equal(capturedEditors.at(-1).items[0].items.map(item => item.name).join(','), 'cpus,cpuunits');
const notesView = { canEdit: false, callParent() { throw new Error('read-only Notes editor opened'); } };
assert.doesNotThrow(() => definitions['PVE.betterdocker.Notes'].run_editor.call(notesView));
context.Proxmox.UserName = 'root@pam';
console.log('Delegated CPU editor excludes memory and disk; read-only Notes cannot open its editor');

const startupEditors = [];
context.Ext.create = (name, options) => { startupEditors.push({ name, options }); return { show() {} }; };
loadDigest(editorDigest, { configure: 0, options: 0, cpu: 1 });
const lifecycleRows = digestPanel.items.find(item => item.itemId === 'options').items[0];
lifecycleRows.listeners.itemdblclick(null, { get: () => 'Start/Shutdown order' });
assert.equal(startupEditors.length, 0);
loadDigest(editorDigest, { configure: 0, options: 1 });
lifecycleRows.listeners.itemdblclick(null, { get: () => 'Start/Shutdown order' });
assert.equal(startupEditors[0].name, 'PVE.window.StartupEdit');
assert(startupEditors[0].options.url.endsWith('/config'));
lifecycleRows.listeners.itemdblclick(null, { get: () => 'Start at boot' });
assert.equal(startupEditors[1].options.items[0].name, 'onboot');
console.log('Native startup editor and Start at boot require Options privileges');

for (const action of ['startall', 'stopall', 'migrateall', 'suspendall', 'start', 'shutdown', 'migrate', 'suspend', 'backup']) {
    let rendered, loaded, removed, initialRemoved;
    const window = { action, nodename: 'pve', down() { return null; } };
    const selector = { columnsDeclaration: [{ dataIndex: 'type', renderer: value => 'native-' + value }],
        callParent() {}, on(event, callback) { rendered = callback; }, up() { return window; },
        getStore() { return { getRange: () => [{ get: () => 'docker' }, { get: () => 'lxc' }],
            remove(records) { initialRemoved = records; } }; }, mon(store, event, callback) { loaded = callback; } };
    definitions['PVE.betterdocker.VMSelectorIntegration'].initComponent.call(selector);
    rendered();
    const docker = { get() { return 'docker'; } }, lxc = { get() { return 'lxc'; } };
    loaded({ remove(records) { removed = records; } }, [docker, lxc]);
    assert.equal(selector.columnsDeclaration[0].renderer('docker'), 'DCT');
    assert.equal(selector.columnsDeclaration[0].renderer('lxc'), 'native-lxc');
    assert.equal(removed?.length || 0, action === 'backup' ? 1 : 0);
    assert.equal(initialRemoved?.length || 0, action === 'backup' ? 1 : 0,
        'already loaded DCTs must be filtered before the next load');
}
console.log('Native bulk selectors include DCT for node and cluster operations');

// Network editor keeps native form/load/task handling and exposes supported fields only.
let networkLoad, loadedNetwork, mtuCleared;
const networkEditor = { nodename: 'pve', callParent() {}, load(options) { networkLoad = options; },
    setValues(values) { loadedNetwork = values; }, down() { return { setValue(value) { mtuCleared = value; } }; } };
definitions['PVE.betterdocker.NetworkEdit'].initComponent.call(networkEditor);
networkLoad.success({ result: { data: { bridge: 'vmbr0', ipv4mode: 'static', cidr: '192.0.2.250/20', mtu: 0, digest: editorDigest } } });
assert.equal(loadedNetwork.cidr, '192.0.2.250/20');
assert.equal(networkEditor.digest, editorDigest);
assert.equal(mtuCleared, '');
const networkFields = [...networkEditor.items[0].column1, ...networkEditor.items[0].column2, ...networkEditor.items[0].advancedColumn1];
for (const name of ['bridge', 'hwaddr', 'cidr', 'mtu']) assert(networkFields.some(field => field.name === name));
for (const name of ['ip6', 'tag', 'rate', 'gw']) assert(!networkFields.some(field => field.name === name));
assert.equal(definitions['PVE.betterdocker.NetworkEdit'].showTaskViewer, false);
assert.equal(networkEditor.items[0].onGetValues({ ipv4mode: 'dhcp', mtu: '' }).cidr, '');
console.log('Network editor uses native load/digest/task handling and supported fields only');

const contentParams = {};
const nativeImage = { store: { getProxy: () => ({ setExtraParam: (key, value) => { contentParams[key] = value; } }) } };
overrides.get(storageImageClass).initComponent.call(nativeImage);
assert(nativeImage.nativeInitialized);
assert.equal(contentParams.dct, 0);

const storageMenu = { pveSelNode: { data: { type: 'storage' } }, items: [{ itemId: 'contentRootdir' }, { itemId: 'permissions' }] };
overrides.get(context.PVE.panel.Config).initComponent.call(storageMenu);
assert(storageMenu.nativeInitialized);
assert.equal(storageMenu.items[1].title, 'Docker Volumes');
assert.equal(storageMenu.items[1].xtype, 'pveBetterDockerStorageVolumes');
assert.equal(storageMenu.items[1].itemId, 'contentDocker');
const otherMenu = { pveSelNode: { data: { type: 'storage' } }, items: [{ itemId: 'contentIso' }] };
overrides.get(context.PVE.panel.Config).initComponent.call(otherMenu);
assert.equal(otherMenu.items.length, 1);
const guestMenu = { pveSelNode: { data: { type: 'docker' } }, items: [{ itemId: 'contentRootdir' }] };
overrides.get(context.PVE.panel.Config).initComponent.call(guestMenu);
assert.equal(guestMenu.items.length, 1);
console.log('Storage categories separate Docker disks and are inserted before native menu initialization');

let guestActionRequest, guestActionWindow;
context.Proxmox.Utils.API2Request = options => { guestActionRequest = options; };
context.Ext.create = (name, options) => { guestActionWindow = { name, options }; return {}; };
commandMenu.items.find(item => item.itemId === 'migrate').handler();
assert(guestActionRequest.url.endsWith('/management'), 'context action must load capabilities and digest');
guestActionRequest.success({ result: { data: { status: 'stopped', name: 'other', digest: 'current-version', capabilities: { configure: 1 } } } });
assert.equal(guestActionWindow.name, 'PVE.betterdocker.Migrate');
assert.equal(guestActionWindow.options.projectId, 'c'.repeat(32));
assert.equal(guestActionWindow.options.digest, 'current-version');
guestActionWindow = undefined;
guestActionRequest.success({ result: { data: { status: 'running', capabilities: { configure: 1 } } } });
assert.equal(guestActionWindow, undefined, 'a running guest cannot open stopped migration');
console.log('Context guest actions load current capabilities, state and configuration digest');

for (const clone of [false, true]) {
    const restoreEditor = { callParent, projectId: clone ? 'a'.repeat(32) : undefined, nodename: 'pve', vmid: 100 };
    definitions['PVE.betterdocker.StateRestore'].initComponent.call(restoreEditor);
    assert.equal(restoreEditor.showTaskViewer, !clone, 'restore shows logs; clone closes quietly like LXC');
    assert.equal(typeof restoreEditor.apiCallDone, clone ? 'function' : 'undefined');
}
assert.equal(definitions['PVE.betterdocker.Migrate'].showTaskViewer, true);
console.log('Native clone, restore and migration feedback policies passed');

// The same row action handles keyboard and double click, and one live editor
// survives refreshes and failed submissions until explicitly closed.
const pendingEditWindows = [];
context.Ext.create = (name, options) => {
    const events = {}, form = { isValid: () => true, isDirty: () => true,
        getValues: () => ({ memory: 128, swap: 0 }) };
    const win = Object.assign({ formPanel: { getForm: () => form },
        on(event, callback) { events[event] = callback; }, show() {}, toFront() { this.focused = true; },
        close() { this.destroy(); }, destroy() { this.destroyed = true; events.destroy?.(); } }, options);
    pendingEditWindows.push(win); return win;
};
digestPanel.dockerEditors = {};
digestPanel.pendingTask = null;
context.Proxmox.Utils.API2Request = options => digestRequests.push(options);
loadDigest(editorDigest);
const memoryRow = { get: () => 'Memory' };
resourceRows.listeners.itemdblclick(null, memoryRow);
const guardedEditor = pendingEditWindows.at(-1);
resourceRows.listeners.itemdblclick(null, memoryRow);
let stoppedKey = false;
resourceRows.listeners.itemkeydown(null, memoryRow, null, 0, { ENTER:13, getKey: () => 13, stopEvent() { stoppedKey = true; } });
assert.equal(pendingEditWindows.length, 1);
assert.equal(guardedEditor.focused, true);
assert.equal(stoppedKey, true);
const requestCount = digestRequests.length;
guardedEditor.formPanel.getForm().isDirty = () => false;
guardedEditor.submit();
assert.equal(digestRequests.length, requestCount, 'unchanged forms do not submit');
guardedEditor.formPanel.getForm().isDirty = () => true;
guardedEditor.submit();
const submitted = digestRequests.at(-1);
guardedEditor.submit();
assert.equal(digestRequests.at(-1), submitted, 'Enter twice cannot submit twice');
const previousEditAlert = context.Ext.Msg.alert, conflictAlerts = [];
context.Ext.Msg.alert = (title, message) => conflictAlerts.push(message);
submitted.failure({ htmlStatus: 'checksum mismatch' });
context.Ext.Msg.alert = previousEditAlert;
assert.deepEqual(conflictAlerts, ['checksum mismatch']);
submitted.callback();
assert.equal(guardedEditor.destroyed, undefined, 'conflict keeps the editor and its input');
assert.equal(guardedEditor.dockerSubmitting, false);
guardedEditor.submit();
assert.notEqual(digestRequests.at(-1), submitted, 'a failed request can be retried');
guardedEditor.destroy();
resourceRows.listeners.itemdblclick(null, memoryRow);
assert.equal(pendingEditWindows.length, 2, 'closing allows a fresh editor');
pendingEditWindows.at(-1).destroy();
const pendingAddressRenderer = networkCard.items[0].columns.find(column => column.dataIndex === 'address').renderer;
assert.equal(pendingAddressRenderer(null, { column: { dataIndex: 'address' } }, {
    data: { address:'192.0.2.216', cidr:'192.0.2.216/20', pending: { cidr:'192.0.2.217/20' } },
}), '192.0.2.216/20<div style="color:darkorange">192.0.2.217/20</div>');
let nativeSnapshotLoaded;
definitions['PVE.betterdocker.SnapshotEdit'].load.call({ callParent(args) {
    args[0].success({ result: { data: { digest:'a'.repeat(64), description:'notes' } } });
} }, { success(response) { nativeSnapshotLoaded = response.result.data; } });
assert.equal(nativeSnapshotLoaded.digest, undefined, 'digest is not shown as snapshot configuration');
assert.equal(nativeSnapshotLoaded.description, 'notes');
console.log('Shared keyboard editing, duplicate prevention, unchanged forms, conflict retry and pending network values passed');

const restoredEditor = { restoredNetwork:true, callParent, load() {}, down() { return null; } };
definitions['PVE.betterdocker.NetworkEdit'].initComponent.call(restoredEditor);
const restoredFields = [...restoredEditor.items[0].column1, ...restoredEditor.items[0].column2, ...restoredEditor.items[0].advancedColumn1];
assert(restoredFields.filter(item => item.name !== 'firewall').every(item => item.disabled));
assert(!restoredFields.find(item => item.name === 'firewall').disabled);
assert.deepEqual(JSON.parse(JSON.stringify(restoredEditor.items[0].onGetValues({ firewall:1, mtu:1400 }))), { firewall:1 });
console.log('Restored endpoints expose only their supported firewall edit');
