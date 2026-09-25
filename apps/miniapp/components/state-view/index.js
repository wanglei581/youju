/* Loading / empty / error / info block with up to two actions. */
Component({
  options: { addGlobalClass: true },
  properties: {
    type: { type: String, value: 'empty' }, // loading | empty | error | info
    title: { type: String, value: '' },
    desc: { type: String, value: '' },
    glyph: { type: String, value: '' },
    actionText: { type: String, value: '' },
    secondaryText: { type: String, value: '' },
    compact: { type: Boolean, value: false },
  },
  methods: {
    onAction() {
      this.triggerEvent('action');
    },
    onSecondary() {
      this.triggerEvent('secondary');
    },
  },
});
