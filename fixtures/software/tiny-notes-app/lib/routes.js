"use strict";
// Route table: "METHOD /path" -> handler(req, res, deps)
const routes = {
  "GET /notes": (req, res, { store, renderList }) => {
    res.writeHead(200, { "content-type": "text/plain" });
    res.end(renderList(store.list()));
  },
  "GET /notes/welcome": (req, res, { store, renderNote }) => {
    const note = store.get("welcome");
    res.writeHead(note ? 200 : 404, { "content-type": "text/plain" });
    res.end(note ? renderNote(note) : "missing");
  },
  "POST /notes": (req, res, { store }) => {
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", () => {
      const { title, text } = JSON.parse(body || "{}");
      res.writeHead(title ? 201 : 400);
      res.end(title ? JSON.stringify(store.add(title, text || "")) : "title required");
    });
  },
};

module.exports = { routes };
