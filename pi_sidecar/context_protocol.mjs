// Private, bounded Context transport. No filesystem or Provider conversion.
import { createHash, randomUUID } from "node:crypto";
const hash = data => createHash("sha256").update(data).digest("hex");
const exact = (value, keys) => Object.keys(value).sort().join() === keys.sort().join();
export class ContextBridge {
  constructor(start, send) {
    this.start = start;
    this.send = message => send({turnId: start.turnId, ...message});
    this.limits = start.contextLimits;
    if (start.contextProtocol !== 1 || !exact(this.limits, ["maxTransferBytes", "chunkBytes", "maxChunks"]) ||
        !Object.values(this.limits).every(Number.isInteger) || this.limits.chunkBytes < 1 || this.limits.chunkBytes > 8192 ||
        this.limits.maxChunks < 1 || this.limits.maxChunks > 128 || this.limits.maxTransferBytes < 1 ||
        this.limits.maxTransferBytes > Math.min(1048576, this.limits.chunkBytes * this.limits.maxChunks)) throw new Error("context limits");
  }
  transfer(direction, messages, key) {
    if (this.pending) throw new Error("overlapping context");
    const requestId = randomUUID();
    const data = Buffer.from(JSON.stringify({messages}));
    const count = Math.ceil(data.length / this.limits.chunkBytes);
    if (data.length > this.limits.maxTransferBytes || count > this.limits.maxChunks) throw new Error("context transfer limit");
    const sha256 = hash(data);
    const promise = new Promise((resolve, reject) => { this.pending = {requestId, key, direction, resolve, reject}; });
    this.send({type: `context.${direction}.begin`, requestId, modelCallKey: key, contextProtocol: 1, schemaVersion: 1, bytes: data.length, chunkCount: count, sha256});
    for (let index = 0; index < count; index++) this.send({type: `context.${direction}.chunk`, requestId, index, data: data.subarray(index * this.limits.chunkBytes, (index + 1) * this.limits.chunkBytes).toString("base64")});
    this.send({type: `context.${direction}.end`, requestId, bytes: data.length, sha256});
    return promise;
  }
  consumeLocal(ready) {
    if (this.pending) throw new Error("overlapping local consume");
    const requestId = randomUUID();
    const promise = new Promise((resolve, reject) => { this.pending = {requestId, direction: "local", resolve, reject}; });
    this.send({type: "context.local.consume", requestId, snapshotId: ready.snapshotId, modelCallKey: ready.modelCallKey, projectionHash: ready.projectionHash});
    return promise;
  }
  receive(message) {
    const {protocolVersion, turnId, seq, ...event} = message;
    const p = this.pending;
    if (!p || event.requestId !== p.requestId) throw new Error("unexpected context response");
    if (event.type === "context.local.accepted") {
      if (p.direction !== "local" || !exact(event, ["type", "requestId"])) throw new Error("local context mismatch");
      this.pending = undefined; p.resolve(); return;
    }
    if (event.type === "context.committed") {
      if (p.direction !== "final" || !exact(event, ["type", "requestId"])) throw new Error("context commit mismatch");
      this.pending = undefined; p.resolve(); return;
    }
    if (p.direction !== "input") throw new Error("unexpected context output");
    if (event.type === "context.output.begin") {
      if (p.header || !exact(event, ["type", "requestId", "modelCallKey", "contextProtocol", "schemaVersion", "bytes", "chunkCount", "sha256", "snapshotId"]) ||
          event.modelCallKey !== p.key || event.contextProtocol !== 1 || event.schemaVersion !== 1 ||
          !/^[a-f0-9-]{36}$/.test(event.snapshotId) || !/^[a-f0-9]{64}$/.test(event.sha256) ||
          !Number.isInteger(event.bytes) || event.bytes < 1 || event.bytes > this.limits.maxTransferBytes ||
          !Number.isInteger(event.chunkCount) || event.chunkCount < 1 || event.chunkCount > this.limits.maxChunks ||
          event.bytes > event.chunkCount * this.limits.chunkBytes) throw new Error("context header invalid");
      p.header = event; p.chunks = []; p.bytes = 0; return;
    }
    if (event.type === "context.output.chunk") {
      if (!p.header || p.projection || !exact(event, ["type", "requestId", "index", "data"]) || event.index !== p.chunks.length ||
          p.chunks.length >= p.header.chunkCount || typeof event.data !== "string" || event.data.length > 4 * Math.ceil(this.limits.chunkBytes / 3)) throw new Error("context chunk invalid");
      const data = Buffer.from(event.data, "base64");
      if (data.toString("base64") !== event.data || data.length < 1 || data.length > this.limits.chunkBytes || (p.bytes += data.length) > p.header.bytes) throw new Error("context bytes invalid");
      p.chunks.push(data); return;
    }
    if (event.type === "context.output.end") {
      if (!p.header || p.projection || !exact(event, ["type", "requestId", "bytes", "sha256"]) || p.chunks.length !== p.header.chunkCount ||
          p.bytes !== p.header.bytes || event.bytes !== p.header.bytes || event.sha256 !== p.header.sha256) throw new Error("context end invalid");
      const data = Buffer.concat(p.chunks);
      if (hash(data) !== p.header.sha256) throw new Error("context hash invalid");
      p.projection = JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(data)); return;
    }
    if (event.type === "context.ready") {
      if (!p.projection || !exact(event, ["type", "requestId", "snapshotId", "modelCallKey", "projectionHash", "policyHash", "epoch", "budget"]) ||
          event.snapshotId !== p.header.snapshotId || event.modelCallKey !== p.key || !/^[a-f0-9]{64}$/.test(event.projectionHash) ||
          typeof event.policyHash !== "string" || !Number.isInteger(event.epoch) || event.epoch < 0 || !Array.isArray(p.projection.messages)) throw new Error("context ready invalid");
      this.pending = undefined; p.resolve({projection: p.projection, ready: event}); return;
    }
    throw new Error("context event invalid");
  }
}
