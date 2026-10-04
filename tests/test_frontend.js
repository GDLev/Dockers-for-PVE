// Check actual wizard construction against an ExtJS API stub.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
let definition;
let override;
const nativeTabs = [{ column1: [{ name: 'vmid' }, { name: 'unprivileged' }, { name: 'features' }] },
    { title: 'Network' }, { onSubmit() {}, dockedItems: [{ name: 'start' }] }];
const clone = value => Array.isArray(value) ? value.map(clone) : value && typeof value === 'object' ?
    Object.fromEntries(Object.entries(value).map(([key, item]) => [key, clone(item)])) : value;
const context = {
    console, window: null,
    PVE: { lxc: { CreateWizard: { prototype: { items: nativeTabs } } }, StdWorkspace: {},
        Utils: { get_object_icon_class: () => 'fa fa-cube running' } },
    Proxmox: { UserName: 'root@pam' },
    Ext: { clone, define: (_, value) => { definition = value; }, override: (_, value) => { override = value; } },
};
context.window = context;
vm.runInNewContext(fs.readFileSync('frontend/betterdocker.js', 'utf8'), context);
const wizard = { callParent() { this.initialized = true; } };
definition.initComponent.call(wizard);
assert(wizard.initialized);
assert.deepEqual(wizard.items.slice(-4, -1).map(tab => tab.title), ['Docker Engine', 'Porty', 'Docker Compose']);
assert.equal(nativeTabs.length, 3); // native wizard must stay untouched
assert.equal(nativeTabs[0].column1.length, 3);
assert.equal(wizard.items.at(-1).dockedItems.length, 0);
assert.equal(context.PVE.Utils.get_object_icon_class('lxc', { type: 'lxc', tags: 'other;betterdocker' }), 'fa bd-docker-icon running');
assert.equal(context.PVE.Utils.get_object_icon_class('lxc', { type: 'lxc', tags: 'other' }), 'fa fa-cube running');
assert(override.initComponent);
console.log('Frontend construction and icon checks passed');
