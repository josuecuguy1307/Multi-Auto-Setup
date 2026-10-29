"use strict";
function renderNote(note) {
  return `# ${note.title}\n\n${note.body}\n`;
}

function renderList(notes) {
  return notes.map((n) => `- ${n.title}`).join("\n") + "\n";
}

module.exports = { renderNote, renderList };
