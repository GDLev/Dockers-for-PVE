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
const context = { window: null, console,
    PVE: { panel: { Config: { prototype: { activateCard() {} } } }, window: { Wizard: {} }, StdWorkspace: workspaceClass, tree: { ResourceTree: tree },
        Utils: { get_object_icon_class: () => 'fa fa-cube' } },
    Proxmox: { NodeName: 'pve', UserName: 'root@pam' },
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
    ['Summary', 'Console', 'Resources', 'Network', 'DNS', 'Options', 'Deployment', 'Logs', 'Task History']);
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
