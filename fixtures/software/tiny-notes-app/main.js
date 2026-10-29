"use strict";
// Entry point of the fixture app. Wires a store to a tiny HTTP server.
const http = require("node:http");
const { createStore } = require("./lib/store");
const { renderNote, renderList } = require("./lib/format");
const { routes } = require("./lib/routes");

const store = createStore();
store.add("welcome", "Hello from the fixture app");

const server = http.createServer((req, res) => {
  const handler = routes[`${req.method} ${req.url.split("?")[0]}`];
  if (!handler) {
    res.writeHead(404, { "content-type": "text/plain" });
    res.end("not found");
    return;
  }
  handler(req, res, { store, renderNote, renderList });
});

module.exports = { server, store };
