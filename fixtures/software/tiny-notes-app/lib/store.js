"use strict";
// In-memory note store.
function createStore() {
  const notes = new Map();
  return {
    add(title, body) {
      notes.set(title, { title, body, createdAt: 0 });
      return notes.get(title);
    },
    get(title) {
      return notes.get(title) || null;
    },
    list() {
      return Array.from(notes.values());
    },
    remove(title) {
      return notes.delete(title);
    },
  };
}

module.exports = { createStore };
