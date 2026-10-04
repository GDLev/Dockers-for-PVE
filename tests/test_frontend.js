const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const definitions = {};
const overrides = new Map();
function callParent() {
    assert.ok(callParent.caller, 'ExtJS callParent must have a non-strict caller');
    this.initialized = true;
}
const tree = { typeDefaults: {}, prototype: { addChildSorted: (node, info) => info } };
const workspaceClass = { prototype: { initComponent() { this.initialized = true; }, setContent(comp) { this.content = comp; } } };
const context = { window: null, console,
    PVE: { window: { Wizard: {} }, StdWorkspace: workspaceClass, tree: { ResourceTree: tree },
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
