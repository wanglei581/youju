/* Result card for one published record (view model from utils/labels.decorateResource).
 * Events: toggle { id, title } · open { id } · copy { url } */
Component({
  options: { addGlobalClass: true },
  properties: {
    item: { type: Object, value: {} },
    selected: { type: Boolean, value: false },
    selectable: { type: Boolean, value: true },
  },
  methods: {
    onToggle() {
      const item = this.data.item;
      this.triggerEvent('toggle', { id: item.id, title: item.title });
    },
    onOpen() {
      this.triggerEvent('open', { id: this.data.item.id });
    },
    onCopy() {
      this.triggerEvent('copy', { url: this.data.item.originalUrl });
    },
  },
});
